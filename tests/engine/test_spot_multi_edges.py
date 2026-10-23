"""Adversarial cash, ownership, lifecycle and recovery regressions."""

from decimal import Context, Decimal, localcontext

import pytest

from tests.engine.test_spot_multi_contract import (
    api,
    binding,
    event,
    instrument,
    order,
    policy,
    recipe,
    session,
)
from tests.engine.test_spot_multi_dca import closed, consume, find, observe, reconcile
from tests.engine.test_spot_multi_paper import activate, candle, fills, paper


def custom_state(kind="dca", costs=None, **params):
    costs = costs or policy()
    return api().create_spot_session(
        binding("1000" if kind == "dca" else "200", costs=costs),
        recipe(kind, **params),
        instrument(),
        costs,
    )


def test_all_in_budget_includes_buy_fee_reservations():
    costs = policy().model_copy(
        update={
            "maker_fee_bps": Decimal(10),
            "taker_fee_bps": Decimal(10),
            "buy_quote_fee_cap_bps": Decimal(10),
        }
    )
    with pytest.raises(ValueError, match="budget|cash"):
        custom_state(costs=costs, budget_quote="200")
    state = custom_state(costs=costs, budget_quote="201", fee_reserve_quote="0.5")
    state = closed(state, "100")
    report = api().spot_execution_report(state, mark="100")
    assert report["quote_reserved"] == "200.7"
    assert report["free_quote"] == "0.3"


def test_buy_base_fee_changes_unit_cost_but_not_gross_vwap():
    costs = policy().model_copy(
        update={
            "fee_asset": "BTC",
            "maker_fee_bps": Decimal(10),
            "taker_fee_bps": Decimal(10),
            "sell_base_fee_cap_bps": Decimal(10),
        }
    )
    state = custom_state(costs=costs)
    state = api().submit_spot_order(state, order(state, "buy", quantity="1", price="100"))
    fill = event(
        state,
        "buy",
        qty="1",
        price="100",
        status="filled",
        fees=(api().SpotFeeV1(asset="BTC", quantity="0.001"),),
    )
    state = api().apply_spot_event(state, fill)
    report = api().spot_execution_report(state, mark="100")
    assert report["gross_average"] == "100"
    assert report["base_quantity"] == "0.999"
    assert report["inventory_cost"] == "100"
    assert Decimal(report["fee_adjusted_unit_cost"]) > 100
    assert report["net_equity"] == "999.9"


def test_quote_fees_are_capitalized_once_and_sell_fees_reduce_proceeds():
    costs = policy().model_copy(
        update={
            "maker_fee_bps": Decimal(10),
            "taker_fee_bps": Decimal(10),
            "buy_quote_fee_cap_bps": Decimal(10),
        }
    )
    state = custom_state(costs=costs)
    state = api().submit_spot_order(state, order(state, "buy", quantity="1", price="100"))
    state = api().apply_spot_event(
        state,
        event(
            state,
            "buy",
            qty="1",
            price="100",
            status="filled",
            fees=(api().SpotFeeV1(asset="USDT", quantity="0.1"),),
        ),
    )
    state = api().reconcile_spot_state(state, api().paper_reconciliation(state))
    state = api().submit_spot_order(
        state, order(state, "sell", side="sell", quantity="1", price="110")
    )
    state = api().apply_spot_event(
        state,
        event(
            state,
            "sell",
            qty="1",
            price="110",
            status="filled",
            seq=2,
            fees=(api().SpotFeeV1(asset="USDT", quantity="0.11"),),
        ),
    )
    report = api().spot_execution_report(state, mark="110")
    assert report["quote_cash"] == "1009.79"
    assert report["realized_profit"] == "9.79"


def test_late_third_asset_valuation_matches_valuation_before_sale():
    costs = policy().model_copy(
        update={
            "fee_asset": "BNB",
            "maker_fee_bps": Decimal(10),
            "taker_fee_bps": Decimal(10),
            "third_asset_quote_price": Decimal(100),
            "third_asset_allowance_quote": Decimal(10),
        }
    )
    state = custom_state(costs=costs)
    state = api().submit_spot_order(state, order(state, "buy", quantity="2", price="100"))
    state = api().apply_spot_event(
        state,
        event(
            state,
            "buy",
            qty="2",
            price="100",
            status="filled",
            fees=(api().SpotFeeV1(asset="BNB", quantity="0.002"),),
        ),
    )
    valuation = api().SpotFeeValuationV1(
        valuation_id="value",
        account_id=state.binding.account_id,
        session_id="s",
        fee_id="e1:0",
        asset="BNB",
        quantity="0.002",
        quote_value="0.2",
        valuation_timestamp_ms=10,
        source="synthetic",
        evidence_id="fx",
        content_sha256="a" * 64,
    )
    early = api().apply_spot_fee_valuation(state, valuation)

    def sale(s):
        s = api().reconcile_spot_state(s, api().paper_reconciliation(s))
        s = api().submit_spot_order(s, order(s, "sell", side="sell", quantity="1", price="110"))
        return api().apply_spot_event(
            s, event(s, "sell", qty="1", price="110", status="filled", seq=2)
        )

    early = sale(early)
    late = api().apply_spot_fee_valuation(sale(state), valuation)
    assert late.account.remaining_cost == early.account.remaining_cost == Decimal("100.1")
    assert late.account.realized_profit == early.account.realized_profit == Decimal("9.9")
    assert api().spot_execution_report(late, mark="100")["net_equity"] == "1009.8"
    assert api().apply_spot_fee_valuation(late, valuation) == late


def test_unvalued_fees_block_new_buys_until_valuation_and_reconciliation():
    costs = policy().model_copy(
        update={
            "fee_asset": "BNB",
            "third_asset_quote_price": Decimal(100),
            "third_asset_allowance_quote": Decimal(10),
        }
    )
    state = custom_state(costs=costs)
    state = api().submit_spot_order(state, order(state, "buy", quantity="1", price="100"))
    state = api().apply_spot_event(
        state,
        event(
            state,
            "buy",
            qty="1",
            price="100",
            status="filled",
            fees=(api().SpotFeeV1(asset="BNB", quantity="0.001"),),
        ),
    )
    assert api().spot_execution_report(state, mark="100")["net_equity"] is None
    with pytest.raises(ValueError, match="fee|reconcil"):
        api().submit_spot_order(state, order(state, "another", quantity="1", price="100"))


def test_beyond_cap_fee_is_quarantined_without_stealing_other_reservations():
    state = session()
    state = api().submit_spot_order(state, order(state, "bid"))
    bad = event(
        state,
        "bid",
        qty="1",
        price="10",
        status="partial",
        fees=(api().SpotFeeV1(asset="USDT", quantity="1"),),
    )
    candidate = api().apply_spot_event(state, bad)
    assert candidate.account == state.account
    assert candidate.orders == state.orders
    assert candidate.quarantine and candidate.reconciliation_required
    assert api().spot_execution_report(candidate, mark="10")["net_equity"] is None


def test_terminal_barrier_consumes_raced_buys_and_pending_sell_before_close():
    state = closed(session("dca"), "100")
    state = consume(state, find(state, "base")[0].intent.client_order_id, "1", "100")
    state = reconcile(state)
    target = find(state, "target")[0]
    addition = find(state, "addition")[0]
    state = observe(
        state, "command", {"command_id": "stop", "action": "stop_and_flatten", "reason": "manual"}
    )
    assert find(state, "flatten") == []
    state = consume(state, addition.intent.client_order_id, "1.25", "80", "canceled")
    state = consume(state, target.intent.client_order_id, "0.5", "110", "canceled")
    state = reconcile(state)
    close = find(state, "flatten")[0]
    assert close.intent.quantity == Decimal("1.75")
    state = observe(
        state, "command", {"command_id": "stop", "action": "stop_and_flatten", "reason": "manual"}
    )
    assert len(find(state, "flatten")) == 1


def test_pending_cancel_generation_cannot_be_replaced_by_stale_request():
    state = session()
    state = api().submit_spot_order(state, order(state, "bid"))
    with pytest.raises(ValueError, match="stale"):
        api().cancel_spot_order(state, "bid", command_id="wrong", generation=1)


def test_out_of_order_deltas_resume_only_after_complete_attributed_reconciliation():
    state = session()
    state = api().submit_spot_order(state, order(state, "bid"))
    first = event(state, "bid", qty="1", price="10", status="partial")
    second = event(state, "bid", qty="1", price="10", status="partial", seq=2).model_copy(
        update={"cumulative_quantity": Decimal(2), "cumulative_quote": Decimal(20)}
    )
    held = api().apply_spot_event(state, second)
    ordered = api().apply_spot_event(api().apply_spot_event(state, first), second)
    proof = api().paper_reconciliation(ordered).model_copy(update={"events": (first,)})
    resumed = api().reconcile_spot_state(held, proof)
    assert resumed.account.net_base == 2
    assert not resumed.reconciliation_required


def test_gap_crossing_grid_levels_prices_at_open_with_no_unowned_sells():
    p = paper()
    activate(p)
    p.on_candle(candle(2, "8.5", low="8.5", high=11, close=11))
    assert [e.price for e in fills(p, "grid_buy")] == [Decimal("8.5"), Decimal("8.5")]
    assert fills(p, "grid_sell") == []


def test_stop_beats_intrabar_target_but_open_target_precedes_later_stop():
    def held():
        p = paper("dca")
        activate(p, "100")
        p.on_candle(candle(2, 100))
        return p

    p = held()
    p.on_candle(candle(3, 100, low=69, high=120, close=100))
    assert fills(p, "target") == []
    assert fills(p, "flatten")[0].price == 70
    other = held()
    other.on_candle(candle(3, 120, low=69, high=120, close=100))
    assert fills(other, "target")[0].price == 120
    assert fills(other, "flatten") == []


def test_dust_remains_visible_after_terminal_flatten():
    rules = instrument().model_copy(
        update={"quantity_step": Decimal("0.1"), "market_quantity_step": Decimal("0.1")}
    )
    costs = policy().model_copy(
        update={
            "fee_asset": "BTC",
            "maker_fee_bps": Decimal(10),
            "taker_fee_bps": Decimal(10),
            "sell_base_fee_cap_bps": Decimal(10),
        }
    )
    p = paper("dca", rules=rules, costs=costs)
    activate(p, "100")
    p.on_candle(candle(2, 100))
    p.command({"command_id": "flatten", "action": "stop_and_flatten", "reason": "manual"})
    p.on_candle(candle(3, 100))
    assert p.state.account.net_base > 0
    assert p.state.controller.phase == "stopped_with_dust"


def test_storage_failure_latches_session_without_dispatching_unacknowledged_intent():
    p = paper()

    def failed(_):
        raise OSError("disk full")

    p._sink = failed
    with pytest.raises(OSError, match="disk full"):
        p.on_candle(candle(0, 10))
    with pytest.raises(RuntimeError, match="storage"):
        p.on_candle(candle(0, 10))
    assert p.state.orders == {}


def test_own_decimal_context_keeps_reservations_and_schema_boundaries_stable():
    with localcontext(Context(prec=6)):
        state = closed(session(), "11")
        assert api().spot_execution_report(state, mark="11")["quote_reserved"] == "190"


def test_completed_dca_can_restart_on_later_signal_but_not_closing_bar():
    p = paper("dca")
    activate(p, "100")
    p.on_candle(candle(2, 100))
    p.on_candle(candle(3, 80))
    p.on_candle(candle(4, 90, high=98))
    assert len([o for o in p.state.orders.values() if o.intent.purpose == "base"]) == 1
    p.on_candle(candle(5, 100))
    assert len([o for o in p.state.orders.values() if o.intent.purpose == "base"]) == 2


def test_incomplete_candle_checkpoint_requires_complete_committed_suffix():
    p = paper()
    records = []
    p._sink = lambda row: records.append(row)
    activate(p)
    start = p.export_state()
    suffix_start = len(records)
    p.on_candle(candle(2, 10))
    suffix = records[suffix_start:]
    incomplete = suffix[0]["state"]
    fresh = paper()
    with pytest.raises(ValueError, match="incomplete|candle"):
        fresh.restore_state(incomplete)
    recovered = paper()
    recovered.restore_state(start, journal_suffix=suffix)
    assert recovered.export_state() == p.export_state()


def test_grid_reports_requested_and_admitted_fixed_quantity_on_rearm():
    rules = instrument().model_copy(update={"quantity_step": Decimal("0.1")})
    p = paper(rules=rules, quantity_per_slot="10.05")
    activate(p)
    buys = [o for o in p.state.orders.values() if o.intent.purpose == "grid_buy"]
    assert {o.intent.quantity for o in buys} == {Decimal(10)}
    assert {o.intent.requested_quantity for o in buys} == {Decimal("10.05")}


def test_impossible_execution_asset_fee_does_not_bypass_declared_cost_cap():
    state = session("dca")
    state = api().submit_spot_order(state, order(state, "buy", quantity="1", price="100"))
    state = api().apply_spot_event(
        state,
        event(
            state,
            "buy",
            qty="1",
            price="100",
            status="filled",
            fees=(api().SpotFeeV1(asset="BTC", quantity="0.5"),),
        ),
    )
    assert state.quarantine
    assert state.account.net_base == 0


def test_public_cancel_replace_is_two_phase_and_rejects_stale_generation():
    state = session()
    old = order(state, "bid")
    state = api().submit_spot_order(state, old)
    replacement = old.model_copy(
        update={
            "client_order_id": "next",
            "intent_id": "next",
            "generation": 1,
            "limit_price": Decimal(9),
        }
    )
    request = api().SpotReplaceIntentV1(
        command_id="replace",
        account_id=state.binding.account_id,
        session_id="s",
        symbol="BTCUSDT",
        client_order_id="bid",
        generation=0,
        replacement=replacement,
    )
    state = api().replace_spot_order(state, request)
    assert state.orders["bid"].status == "cancel_pending"
    assert "next" not in state.orders
    state = api().apply_spot_event(state, event(state, "bid", status="canceled"))
    state = api().reconcile_spot_state(state, api().paper_reconciliation(state))
    state = api().replace_spot_order(state, request)
    assert state.orders["next"].reserved_quote == 90
    with pytest.raises(ValueError, match="stale|generation|conflict"):
        api().replace_spot_order(state, request.model_copy(update={"generation": 1}))


def test_failed_terminal_close_requires_distinct_reconciled_continuation_command():
    state = closed(session("dca"), "100")
    state = consume(state, find(state, "base")[0].intent.client_order_id, "1", "100")
    state = reconcile(state)
    state = observe(
        state, "command", {"command_id": "stop", "action": "stop_and_flatten", "reason": "manual"}
    )
    for pending in list(state.orders.values()):
        if pending.status == "cancel_pending":
            state = observe(
                state,
                "executions",
                {
                    "events": [
                        event(
                            state,
                            pending.intent.client_order_id,
                            status="canceled",
                            seq=state.event_sequence + 1,
                        ).model_dump(mode="json")
                    ]
                },
            )
    state = reconcile(state)
    close = find(state, "flatten")[0]
    state = observe(
        state,
        "executions",
        {
            "events": [
                event(
                    state,
                    close.intent.client_order_id,
                    status="rejected",
                    seq=state.event_sequence + 1,
                ).model_dump(mode="json")
            ]
        },
    )
    state = reconcile(state)
    assert state.controller.phase == "containment"
    assert len(find(state, "flatten")) == 1
    state = observe(
        state,
        "command",
        {"command_id": "resume-close", "action": "stop_and_flatten", "reason": "manual"},
    )
    assert len(find(state, "flatten")) == 2


def test_unknown_outcome_forbids_submit_cancel_and_replacement():
    state = session()
    state = api().submit_spot_order(state, order(state, "bid"))
    state = api().apply_spot_event(state, event(state, "bid", status="unknown"))
    with pytest.raises(ValueError, match="reconcil"):
        api().cancel_spot_order(state, "bid", command_id="cancel", generation=0)
    with pytest.raises(ValueError, match="reconcil"):
        api().submit_spot_order(state, order(state, "new"))


@pytest.mark.parametrize(
    "kind,updates",
    [
        ("dca", {"max_additions": True}),
        ("grid", {"intervals": 65}),
        ("grid", {"version": True}),
        ("dca", {"take_profit_pct": "NaN"}),
        ("dca", {"deviation_pct": "50"}),
    ],
)
def test_strict_recipe_bounds(kind, updates):
    with pytest.raises(ValueError):
        recipe(kind, **updates)


def test_expiry_releases_remaining_reserve_before_matching_and_market_rejection_is_visible():
    costs = policy().model_copy(update={"expires_after_bars": 1})
    p = paper(costs=costs)
    activate(p)
    p.on_candle(candle(2, 11))
    p.on_candle(candle(3, 10))
    assert fills(p) == []
    assert any(e.status == "expired" for e in p.state.events.values())
    rules = instrument().model_copy(update={"market_maximum_quantity": Decimal("0.5")})
    d = paper("dca", rules=rules)
    activate(d, "100")
    d.on_candle(candle(2, 100))
    assert d.state.account.net_base == 0
    assert any(e.status == "rejected" for e in d.state.events.values())
