"""Fresh-review reproductions: uncertainty and first-fill containment."""

from decimal import Decimal

import pytest

from tests.engine.test_spot_multi_contract import api, event, instrument, order, session
from tests.engine.test_spot_multi_paper import activate, candle, paper


def test_unknown_cancel_cannot_be_cleared_by_repeating_unchanged_order_facts():
    state = session()
    state = api().submit_spot_order(state, order(state, "bid"))
    state = api().cancel_spot_order(state, "bid", command_id="cancel", generation=0)
    state = api().apply_spot_event(state, event(state, "bid", status="unknown"))
    assert state.reconciliation_required
    with pytest.raises(ValueError, match="unknown|outcome|reconcil"):
        api().reconcile_spot_state(state, api().paper_reconciliation(state))


def test_first_partial_fill_survives_unadmittable_target_as_containment():
    rules = instrument().model_copy(update={"maximum_price": Decimal(105)})
    p = paper("dca", rules=rules)
    activate(p, "100")
    p.on_candle(candle(2, 100, volume="0.4"))
    assert p.state.account.net_base == Decimal("0.4")
    assert p.state.controller.stop == 70
    assert p.state.controller.terminal_action == "stop_and_flatten"
    assert not any(o.intent.purpose == "addition" for o in p.state.orders.values())


def test_impossible_dca_order_capacity_is_refused_before_any_fill():
    rules = instrument().model_copy(update={"maximum_open_orders": 1})
    with pytest.raises(ValueError, match="capacity|orders"):
        paper("dca", rules=rules)


def test_numeric_execution_ids_are_compared_as_identity_not_money():
    assert (
        not api()
        .compare_spot_execution_reports({"event_id": "1"}, {"event_id": "1.0000000000000000001"})
        .matches
    )


def test_price_crossings_and_closed_candles_cannot_change_or_rewind():
    from tests.engine.test_spot_multi_dca import observe

    state = session()
    state = observe(
        state,
        "price",
        {
            "bar_index": 0,
            "bar_id": "bar",
            "crossing_index": 1,
            "phase": "ascending",
            "price": "100",
        },
    )
    restored = api().restore_spot_state(
        api().export_spot_state(state), expected_binding=state.binding
    )
    for payload in (
        {"bar_index": 0, "bar_id": "bar", "crossing_index": 1, "phase": "open", "price": "80"},
        {"bar_index": 0, "bar_id": "bar", "crossing_index": 2, "phase": "open", "price": "80"},
        {
            "bar_index": 0,
            "bar_id": "different",
            "crossing_index": 2,
            "phase": "ascending",
            "price": "101",
        },
    ):
        with pytest.raises(ValueError, match="crossing|phase|identity|causal"):
            observe(restored, "price", payload)
    with pytest.raises(ValueError, match="candle|OHLCV"):
        observe(
            session(),
            "closed_bar",
            {"bar_index": 0, "close": "100", "start": False, "ohlcv": [0, 100, 90, 110, 100, 1]},
        )
    closed = observe(session(), "closed_bar", {"bar_index": 0, "close": "100", "start": False})
    with pytest.raises(ValueError, match="candle|identity"):
        observe(closed, "closed_bar", {"bar_index": 0, "close": "101", "start": False})


def test_market_notional_exemption_does_not_leave_sellable_inventory_as_dust():
    rules = instrument().model_copy(
        update={"minimum_notional": Decimal(100), "apply_notional_to_market": False}
    )
    p = paper("dca", rules=rules, max_additions=0)
    activate(p, "100")
    p.on_candle(candle(2, 100))
    p.on_candle(candle(3, 69))
    assert p.state.account.net_base == 0
    assert p.state.controller.phase == "stopped_flat"


def test_attributed_overfill_is_quarantined_and_write_latched():
    state = session()
    state = api().submit_spot_order(state, order(state, "bid"))
    candidate = api().apply_spot_event(
        state, event(state, "bid", qty="11", price="10", status="filled")
    )
    assert candidate.account == state.account
    assert candidate.quarantine and candidate.reconciliation_required
    with pytest.raises(ValueError, match="reconcil"):
        api().submit_spot_order(candidate, order(candidate, "new"))


def test_conformance_never_hides_negative_reserve_or_a_whole_lot():
    compare = api().compare_spot_execution_reports
    assert not compare({"base_reserved": "0"}, {"base_reserved": "-0.000000000000000001"}).matches
    assert not compare({"quantity": "1"}, {"quantity": "1.000000000000000001"}).matches
    assert not compare({"price": "1"}, {"price": "1.000000000000000001"}).matches


def test_reports_are_detached_from_trace_quarantine_and_equity_state():
    p = paper()
    activate(p)
    before = p.export_state()
    report = p.report(mark="11")
    report["trace"].append({"kind": "foreign"})
    report["quarantine"].append({"foreign": True})
    report["equity_curve"].clear()
    assert p.export_state() == before


def test_invalid_addition_admission_cannot_discard_confirmed_base_fill():
    rules = instrument().model_copy(update={"minimum_notional": Decimal(50)})
    p = paper("dca", rules=rules, addition_quote="10")
    activate(p, "100")
    p.on_candle(candle(2, 100))
    assert p.state.account.net_base == 1
    assert p.state.controller.stop == 70
    assert p.state.controller.terminal_action == "stop_and_flatten"


def test_quarantined_event_replay_is_neutral_and_conflicting_identity_is_rejected():
    state = session()
    state = api().submit_spot_order(state, order(state, "bid"))
    invalid = event(state, "bid", qty="11", price="10", status="filled")
    state = api().apply_spot_event(state, invalid)
    assert api().apply_spot_event(state, invalid) == state
    with pytest.raises(ValueError, match="conflicting.*event_id"):
        api().apply_spot_event(state, invalid.model_copy(update={"timestamp_ms": 2}))


def test_current_instrument_snapshot_is_explicitly_approximate_in_historical_report():
    from tests.engine.test_spot_multi_contract import binding, policy, recipe

    rules = instrument().model_copy(update={"status": "current_snapshot"})
    identity = binding(rules=rules).model_copy(update={"mode": "historical"})
    state = api().create_spot_session(identity, recipe(), rules, policy())
    report = api().spot_execution_report(state, mark="11")
    assert report["instrument"]["status"] == "current_snapshot"
    assert report["instrument_usage"]["status"] == "approximation"
