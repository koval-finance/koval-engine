"""End-to-end native graph -> controller -> causal paper fills."""

from decimal import Decimal

import pytest

from tests.engine.test_spot_multi_contract import api, binding, instrument, policy
from tests.strategy.graph.test_spot_recipes import graph


def paper(kind="grid", session_id="s", costs=None, rules=None, **params):
    costs = costs or policy()
    rules = rules or instrument()
    g = graph(kind)
    g["blocks"][-1]["params"].update(params)
    return api().SpotPaperSessionV1(
        binding(
            "1000" if kind == "dca" else "200",
            session=session_id,
            graph=g,
            costs=costs,
            rules=rules,
        ),
        g,
        rules,
        costs,
        on_record=lambda row: None,
    )


def candle(index, opening, low=None, high=None, close=None, volume="100"):
    return [
        index * 60000,
        str(opening),
        str(high if high is not None else opening),
        str(low if low is not None else opening),
        str(close if close is not None else opening),
        str(volume),
    ]


def activate(p, start="11"):
    p.on_candle(candle(0, Decimal(start) - 1))
    p.on_candle(candle(1, start))


def fills(p, purpose=None):
    return [
        e
        for e in p.state.events.values()
        if e.quantity and (purpose is None or e.purpose == purpose)
    ]


def test_paper_grid_control_uses_separate_observations_and_shared_controller():
    p = paper()
    activate(p)
    assert p.report(mark="11")["quote_reserved"] == "190"
    p.on_candle(candle(2, 10))
    assert len(fills(p, "grid_buy")) == 1
    assert fills(p, "grid_sell") == []
    p.on_candle(candle(3, 11))
    assert len(fills(p, "grid_sell")) == 1
    p.on_candle(candle(4, 11))
    report = p.report(mark="11")
    assert (
        report["quote_cash"],
        report["base_quantity"],
        report["quote_reserved"],
        report["free_quote"],
        report["realized_profit"],
    ) == ("210", "0", "190", "20", "10")


def test_paper_dca_control_has_actual_average_and_common_tp():
    p = paper("dca")
    activate(p, "100")
    p.on_candle(candle(2, 100))
    p.on_candle(candle(3, 80))
    assert p.report(mark="80")["base_quantity"] == "2.25"
    assert Decimal(p.report(mark="80")["inventory_cost"]) == 200
    p.on_candle(candle(4, 90, high="98"))
    report = p.report(mark="98")
    assert abs(Decimal(report["quote_cash"]) - 1020) < Decimal("1e-15")
    assert report["base_quantity"] == "0"
    assert len(fills(p, "target")) == 1


def test_same_candle_multilevel_uses_one_volume_cap_without_rearm():
    p = paper()
    activate(p)
    p.on_candle(candle(2, 11, low=9, high=12, close=11, volume=12))
    assert sum((e.quantity for e in fills(p)), Decimal(0)) == 12
    assert fills(p, "grid_sell") == []
    assert len([o for o in p.state.orders.values() if o.intent.purpose == "grid_buy"]) == 2
    assert any(row["kind"] == "ambiguity" for row in p.state.trace)


def test_partial_base_stop_cancels_rest_and_holds_unfilled_close_when_cap_exhausted():
    p = paper("dca")
    activate(p, "100")
    p.on_candle(candle(2, 100, low=69, high=100, close=69, volume="0.4"))
    assert p.state.controller.stop == 70
    assert p.state.controller.terminal_action == "stop_and_flatten"
    assert p.state.account.net_base == Decimal("0.4")
    assert not any(o.intent.purpose == "addition" for o in p.state.orders.values())
    p.on_candle(candle(3, 68))
    assert p.state.account.net_base == 0
    assert p.state.controller.phase == "stopped_flat"


def test_reconnect_restore_and_two_sessions_have_independent_state():
    p = paper()
    activate(p)
    p.on_candle(candle(2, 10, volume=3))
    document = p.export_state()
    fresh = paper()
    fresh.restore_state(document)
    fresh.on_candle(candle(2, 10, volume=3))
    assert fresh.export_state() == document
    fresh.on_candle(candle(3, 10))
    assert p.state.account.net_base == 3
    assert fresh.state.account.net_base == 10
    other = paper(session_id="independent")
    assert other.state.account.net_base == 0
    with pytest.raises(ValueError, match="binding"):
        other.restore_state(document)


def test_terminal_hold_cancels_sell_reserve_and_stays_stopped_after_restore():
    p = paper()
    activate(p)
    p.on_candle(candle(2, 10))
    p.command({"command_id": "hold", "action": "stop_and_hold", "reason": "manual"})
    report = p.report(mark="10")
    assert report["base_quantity"] == "10"
    assert report["base_reserved"] == "0"
    assert report["quote_reserved"] == "0"
    assert p.state.controller.phase == "stopped_holding"
    restored = paper()
    restored.restore_state(p.export_state())
    restored.on_candle(candle(3, 7))
    assert restored.state.account.net_base == 10
