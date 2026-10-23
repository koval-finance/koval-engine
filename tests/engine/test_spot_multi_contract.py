"""Independent cash controls for the additive Spot multi-order contract."""

from copy import deepcopy
from decimal import Decimal

import pytest


def api():
    import koval.engine.spot_multi_order as spot

    return spot


def instrument():
    return api().SpotInstrumentEvidenceV1(
        evidence_id="synthetic",
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        source="independent synthetic accounting control",
        status="synthetic_fixture",
        effective_from_ms=0,
        effective_to_ms=1000000000,
        acquired_at_ms=0,
        price_tick="0.000000000000000001",
        quantity_step="0.000000000000000001",
        market_quantity_step="0.000000000000000001",
        minimum_quantity="0.00000001",
        maximum_quantity="1000000",
        minimum_notional="0.000001",
        maximum_notional="1000000",
        minimum_price="0.00000001",
        maximum_price="1000000",
        maximum_open_orders=128,
    )


def policy(**updates):
    return api().SpotFillPolicyV1(
        policy_id="explicit-control",
        maker_fee_bps="0",
        taker_fee_bps="0",
        fee_asset="USDT",
        buy_quote_fee_cap_bps="0",
        sell_base_fee_cap_bps="0",
        spread_bps="0",
        slippage_bps="0",
        maximum_volume_participation="1",
        fee_source="synthetic explicit zero fees",
        **updates,
    )


def recipe(kind="grid", **updates):
    params = (
        dict(
            budget_quote="200",
            fee_reserve_quote="0",
            lower_price="9",
            upper_price="11",
            intervals=2,
            quantity_per_slot="10",
            stop_price="8",
        )
        if kind == "grid"
        else dict(
            budget_quote="1000",
            fee_reserve_quote="0",
            base_quote="100",
            addition_quote="100",
            max_additions=1,
            deviation_pct="20",
            take_profit_pct="10",
            stop_loss_pct="30",
        )
    )
    return api().SpotRecipeV1(kind=kind, params={**params, **updates})


def binding(capital="200", session="s", graph=None, costs=None, rules=None):
    spot = api()
    return spot.SpotBindingV1(
        account_id=f"a-{session}",
        session_id=session,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        timeframe="1m",
        graph_sha256=spot.spot_content_hash(graph or {}),
        software_identity="unreleased-control",
        policy_sha256=spot.spot_content_hash(costs or policy()),
        instrument_sha256=spot.spot_content_hash(rules or instrument()),
        initial_quote_cash=capital,
        end_of_data_policy="stop_and_hold",
    )


def session(kind="grid", **updates):
    return api().create_spot_session(
        binding("1000" if kind == "dca" else "200"), recipe(kind, **updates), instrument(), policy()
    )


def order(state, name, side="buy", quantity="10", price="10", **updates):
    return api().SpotOrderIntentV1(
        account_id=state.binding.account_id,
        session_id=state.binding.session_id,
        symbol="BTCUSDT",
        intent_id=name,
        client_order_id=name,
        cycle_id="c1",
        slot_id=updates.pop("slot_id", name),
        generation=0,
        side=side,
        order_type="limit",
        quantity=quantity,
        limit_price=price,
        purpose="grid_buy" if side == "buy" else "grid_sell",
        activation_bar=1,
        **updates,
    )


def event(state, name, qty="0", price=None, status="accepted", seq=1, **updates):
    record = state.orders[name]
    delta = Decimal(qty)
    notional = delta * Decimal(price or "0")
    return api().SpotExecutionEventV1(
        event_id=f"e{seq}",
        account_id=state.binding.account_id,
        session_id=state.binding.session_id,
        symbol="BTCUSDT",
        client_order_id=name,
        exchange_order_id=f"paper-{name}",
        generation=record.intent.generation,
        source_sequence=seq,
        timestamp_ms=seq,
        side=record.intent.side,
        purpose=record.intent.purpose,
        status=status,
        trade_id=f"t{seq}" if delta else None,
        quantity=qty,
        price=price,
        quote_notional=str(notional),
        cumulative_quantity=str(record.filled_quantity + delta),
        cumulative_quote=str(record.filled_quote + notional),
        **updates,
    )


def test_two_independent_resting_bids_reserve_cash_without_spending():
    spot = api()
    original = session()
    first = spot.submit_spot_order(original, order(original, "bid10"))
    both = spot.submit_spot_order(first, order(first, "bid9", price="9"))
    assert original.orders == {}
    report = spot.spot_execution_report(both, mark="11")
    assert report["quote_cash"] == "200"
    assert report["quote_reserved"] == "190"
    assert report["free_quote"] == "10"
    with pytest.raises(ValueError, match="cash|reserve"):
        spot.submit_spot_order(both, order(both, "overspend"))


def test_fill_duplicate_conflict_and_mismatch_are_atomic():
    spot = api()
    state = session()
    state = spot.submit_spot_order(state, order(state, "bid"))
    fill = event(state, "bid", qty="4", price="10", status="partial")
    filled = spot.apply_spot_event(state, fill)
    assert state.account.net_base == 0
    assert filled.account.net_base == 4
    assert filled.orders["bid"].reserved_quote == 60
    assert spot.apply_spot_event(filled, fill) == filled
    for changes in ({"price": Decimal(9)}, {"account_id": "foreign"}, {"generation": 1}):
        with pytest.raises(ValueError):
            spot.apply_spot_event(filled, fill.model_copy(update=changes))
    assert filled.account.quote_cash == 160


def test_cancel_pending_retains_reserve_and_consumes_raced_fill():
    spot = api()
    state = session()
    state = spot.submit_spot_order(state, order(state, "bid"))
    state = spot.cancel_spot_order(state, "bid", command_id="cancel", generation=0)
    assert state.orders["bid"].reserved_quote == 100
    state = spot.apply_spot_event(state, event(state, "bid", qty="2", price="10", status="partial"))
    state = spot.apply_spot_event(state, event(state, "bid", status="canceled", seq=2))
    assert state.account.net_base == 2
    assert state.account.quote_cash == 180
    assert state.orders["bid"].reserved_quote == 80
    state = spot.reconcile_spot_state(state, spot.paper_reconciliation(state))
    assert state.orders["bid"].reserved_quote == 0


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "1e19", "9" * 129, True])
def test_invalid_and_oversized_inputs_are_refused(value):
    with pytest.raises(ValueError):
        session(quantity_per_slot=value)


def test_out_of_order_event_requires_reconciliation_and_restore_preserves_halt():
    spot = api()
    state = session()
    state = spot.submit_spot_order(state, order(state, "bid"))
    state = spot.apply_spot_event(
        state, event(state, "bid", qty="1", price="10", status="partial", seq=2)
    )
    assert state.reconciliation_required
    assert state.account.net_base == 0
    restored = spot.restore_spot_state(
        spot.export_spot_state(state), expected_binding=state.binding
    )
    assert restored.reconciliation_required
    with pytest.raises(ValueError, match="reconcil"):
        spot.submit_spot_order(restored, order(restored, "another"))
    document = deepcopy(spot.export_spot_state(state))
    document["version"] = "unknown"
    with pytest.raises(ValueError):
        spot.restore_spot_state(document, expected_binding=state.binding)


def test_inventory_sell_reserves_base_and_prevents_double_close():
    spot = api()
    state = session()
    state = spot.submit_spot_order(state, order(state, "bid"))
    state = spot.apply_spot_event(state, event(state, "bid", qty="10", price="10", status="filled"))
    state = spot.reconcile_spot_state(state, spot.paper_reconciliation(state))
    state = spot.submit_spot_order(
        state, order(state, "ask", side="sell", price="11", slot_id="bid")
    )
    assert state.orders["ask"].reserved_base == 10
    with pytest.raises(ValueError, match="inventory|base"):
        spot.submit_spot_order(state, order(state, "duplicate", side="sell", price="11"))
