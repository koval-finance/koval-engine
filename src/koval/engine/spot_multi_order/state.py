"""State construction, reports and bound, checksummed restoration."""

from copy import deepcopy
from decimal import Decimal

from koval.engine.spot_numbers import decimal_text, exact, floor_step
from koval.strategy.schemas import SpotDcaParams, SpotGridParams

from .account import free_base, free_quote, reserved_base, reserved_quote
from .models import (
    ZERO,
    SpotAccountStateV1,
    SpotBindingV1,
    SpotFillPolicyV1,
    SpotInstrumentEvidenceV1,
    SpotRecipeV1,
    SpotSessionStateV1,
    spot_content_hash,
)


@exact
def grid_points(params, instrument):
    points = [
        floor_step(
            params.lower_price + (params.upper_price - params.lower_price) * i / params.intervals,
            instrument.price_tick,
        )
        for i in range(params.intervals + 1)
    ]
    if len(set(points)) != len(points) or points[0] <= 0:
        raise ValueError("normalized duplicate or nonpositive Grid levels")
    if not instrument.minimum_price <= points[0] < points[-1] <= instrument.maximum_price:
        raise ValueError("Grid prices outside instrument evidence")
    if params.stop_price >= points[0]:
        raise ValueError("normalized Grid stop must remain below range")
    return points


@exact
def create_spot_session(binding, recipe, instrument, policy):
    binding = SpotBindingV1.model_validate(binding)
    recipe = SpotRecipeV1.model_validate(recipe)
    instrument = SpotInstrumentEvidenceV1.model_validate(instrument)
    policy = SpotFillPolicyV1.model_validate(policy)
    if binding.policy_sha256 != spot_content_hash(
        policy
    ) or binding.instrument_sha256 != spot_content_hash(instrument):
        raise ValueError("Spot policy/instrument content binding mismatch")
    if (binding.symbol, binding.base_asset, binding.quote_asset) != (
        instrument.symbol,
        instrument.base_asset,
        instrument.quote_asset,
    ):
        raise ValueError("Spot instrument identity mismatch")
    if (
        policy.fee_asset == binding.quote_asset
        and max(policy.maker_fee_bps, policy.taker_fee_bps) > policy.buy_quote_fee_cap_bps
    ):
        raise ValueError("BUY fee reserve cap below configured costs")
    if (
        policy.fee_asset == binding.base_asset
        and max(policy.maker_fee_bps, policy.taker_fee_bps) > policy.sell_base_fee_cap_bps
    ):
        raise ValueError("SELL base fee reserve cap below configured costs")
    if policy.fee_asset not in {binding.base_asset, binding.quote_asset} and (
        not policy.third_asset_quote_price or not policy.third_asset_allowance_quote
    ):
        raise ValueError("third-asset paper fees require valuation evidence and allowance")
    cls = SpotDcaParams if recipe.kind == "dca" else SpotGridParams
    p = cls.model_validate(recipe.params)
    if p.budget_quote > binding.initial_quote_cash:
        raise ValueError("recipe exceeds allocated starting cash")
    if recipe.kind == "dca":
        if instrument.maximum_open_orders < max(2, p.max_additions + 1):
            raise ValueError("insufficient owned order capacity for DCA base/target/additions")
        maximum = (p.base_quote + p.max_additions * p.addition_quote) * (
            1 + policy.buy_quote_fee_cap_bps / 10000
        )
        if maximum + p.fee_reserve_quote > p.budget_quote:
            raise ValueError("all-in DCA budget cannot fund BUYs plus fee reserve")
    else:
        grid_points(p, instrument)
        if floor_step(p.quantity_per_slot, instrument.quantity_step) < instrument.minimum_quantity:
            raise ValueError("normalized Grid quantity below minimum")
    return SpotSessionStateV1(
        binding=binding,
        recipe=recipe,
        instrument=instrument,
        policy=policy,
        account=SpotAccountStateV1(
            quote_cash=binding.initial_quote_cash,
            allocation_cash=p.budget_quote,
            cleanup_reserve=p.fee_reserve_quote,
        ),
        peak_equity=binding.initial_peak_equity or binding.initial_quote_cash,
    )


@exact
def spot_execution_report(state, *, mark):
    from koval.engine.spot_numbers import number

    mark = number(mark)
    if mark <= 0:
        raise ValueError("positive mark required")
    a = state.account
    unvalued = [
        key
        for key, fee in a.fee_records.items()
        if fee["quote_value"] is None and Decimal(fee["quantity"])
    ]
    gross = a.quote_cash + a.net_base * mark
    net = None if unvalued or state.quarantine else gross - a.valued_third_fees
    return deepcopy(
        {
            "version": "koval_spot_execution_report_v1",
            "binding": state.binding.model_dump(mode="json"),
            "quote_cash": decimal_text(a.quote_cash),
            "allocation_cash": decimal_text(a.allocation_cash),
            "quote_reserved": decimal_text(
                reserved_quote(state) + a.cycle_reserve + a.cleanup_reserve
            ),
            "order_quote_reserved": decimal_text(reserved_quote(state)),
            "cycle_quote_reserved": decimal_text(a.cycle_reserve),
            "cleanup_quote_reserved": decimal_text(a.cleanup_reserve),
            "free_quote": decimal_text(free_quote(state)),
            "base_quantity": decimal_text(a.net_base),
            "base_reserved": decimal_text(reserved_base(state)),
            "free_base": decimal_text(free_base(state)),
            "inventory_cost": decimal_text(a.remaining_cost),
            "gross_bought_base": decimal_text(a.bought_base),
            "gross_buy_notional": decimal_text(a.bought_quote),
            "gross_average": decimal_text(a.bought_quote / a.bought_base)
            if a.bought_base
            else None,
            "fee_adjusted_unit_cost": decimal_text(a.remaining_cost / a.net_base)
            if a.net_base
            else None,
            "realized_profit": None
            if unvalued or state.quarantine
            else decimal_text(a.realized_profit),
            "marked_inventory": decimal_text(a.net_base * mark),
            "gross_equity": decimal_text(gross),
            "net_equity": None if net is None else decimal_text(net),
            "unvalued_fees": unvalued,
            "fees_by_asset": {k: decimal_text(v) for k, v in a.fees_by_asset.items()},
            "orders": {key: o.model_dump(mode="json") for key, o in state.orders.items()},
            "lots": {key: lot.model_dump(mode="json") for key, lot in a.lots.items()},
            "basket": state.controller.model_dump(mode="json"),
            "terminal_holding": decimal_text(a.net_base),
            "reconciliation_required": state.reconciliation_required,
            "quarantine": state.quarantine,
            "trace": state.trace,
            "equity_curve": state.equity_curve,
            "maximum_drawdown": decimal_text(state.maximum_drawdown),
            "policy": state.policy.model_dump(mode="json"),
            "instrument": state.instrument.model_dump(mode="json"),
            "instrument_usage": {
                "status": "approximation"
                if state.binding.mode == "historical"
                and state.instrument.status == "current_snapshot"
                else state.instrument.status,
                "evidence_id": state.instrument.evidence_id,
                "source_status": state.instrument.status,
            },
        }
    )


def export_spot_state(state):
    payload = state.model_dump(mode="json")
    return {
        "version": "koval_spot_session_state_v1",
        "state": payload,
        "sha256": spot_content_hash(payload),
    }


@exact
def restore_spot_state(document, *, expected_binding):
    if (
        set(document) != {"version", "state", "sha256"}
        or document["version"] != "koval_spot_session_state_v1"
        or document["sha256"] != spot_content_hash(document["state"])
    ):
        raise ValueError("Spot restore version or integrity mismatch")
    state = SpotSessionStateV1.model_validate(document["state"])
    if state.binding != SpotBindingV1.model_validate(expected_binding):
        raise ValueError("Spot restore binding/software mismatch")
    create_spot_session(state.binding, state.recipe, state.instrument, state.policy)
    if (
        free_quote(state) < 0
        or free_base(state) < 0
        or state.account.net_base
        != sum((lot.quantity for lot in state.account.lots.values()), ZERO)
        or abs(
            state.account.remaining_cost
            - sum((lot.cost for lot in state.account.lots.values()), ZERO)
        )
        > Decimal("1e-240")
    ):
        raise ValueError("Spot restore account/reservation mismatch")
    if state.liquidity_used > state.liquidity_allowance or state.event_sequence != len(
        state.events
    ):
        raise ValueError("Spot restore execution/liquidity mismatch")
    for key, order in state.orders.items():
        if key != order.intent.client_order_id or (
            order.intent.account_id,
            order.intent.session_id,
            order.intent.symbol,
        ) != (state.binding.account_id, state.binding.session_id, state.binding.symbol):
            raise ValueError("Spot restore order identity mismatch")
    return state
