"""Native trailing stop: ``exec.order_constructor.trail_pct`` drives
``GraphStrategy.on_sl_update`` through the existing ``trailing_stop_price``
helper. Legacy dynamic-exit blocks keep their own seam."""

import hashlib
import json

import pytest
from pydantic import ValidationError

import koval.strategy.nodes  # noqa: F401
from koval.strategy.block_assembler import GraphValidationError, assemble_from_graph
from koval.strategy.graph.compat import compile_legacy, extract_dynamic_exit
from koval.strategy.graph.registry import get_node
from koval.strategy.graph.strategy import build_graph_strategy

# The 0.12.3 dump of default exec.order_constructor params.
_RELEASED_DEFAULTS = {
    "sl_pct": 2.0,
    "risk_reward": 2.0,
    "tp_pct": None,
    "risk_pct": 1.0,
    "leverage": 1.0,
    "entry_type": "market",
}


def _native(order_params, *, allow_long=True, allow_short=False):
    return {
        "blocks": [
            {
                "id": "source",
                "type": "fact.every_bar",
                "params": {"direction": "bearish" if allow_short else "bullish"},
            },
            {"id": "confluence", "type": "interp.confluence_and", "params": {}},
            {
                "id": "gate",
                "type": "interp.direction_gate",
                "params": {"allow_long": allow_long, "allow_short": allow_short},
            },
            {"id": "order", "type": "exec.order_constructor", "params": order_params},
        ],
        "connections": [
            {"from": "source", "from_port": "event", "to": "confluence", "to_port": "events"},
            {
                "from": "confluence",
                "from_port": "agreement",
                "to": "gate",
                "to_port": "agreement",
            },
            {"from": "gate", "from_port": "intent", "to": "order", "to_port": "intent"},
        ],
    }


def _legacy(*, exit_params=None, trailing=False):
    blocks = [
        {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 2, "slow": 3}},
        {"id": "ent", "type": "entry.long_only", "params": {"entry_type": "market"}},
        {
            "id": "ex",
            "type": "exit.fixed_sl_tp",
            "params": exit_params or {"sl_pct": 2.0, "risk_reward": 2.0},
        },
    ]
    connections = [{"from": "sig", "to": "ent"}, {"from": "ent", "to": "ex"}]
    previous = "ex"
    if trailing:
        blocks.append({"id": "trail", "type": "exit.trailing_stop", "params": {"trail_pct": 5.0}})
        connections.append({"from": "ex", "to": "trail"})
        previous = "trail"
    blocks.append({"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1.0}})
    connections.append({"from": previous, "to": "rsk"})
    return {"blocks": blocks, "connections": connections}


def _bar(s, close, index=0):
    s.close = s.high = s.low = s.open = float(close)
    s.volume = 1.0
    s.bar_index = index
    s.timestamp_ms = index * 1000
    s.account_value = 10_000.0


def _open(order_params, *, short=False):
    s = assemble_from_graph(_native(order_params, allow_long=not short, allow_short=short))
    _bar(s, 100.0)
    setup = s.go_short() if short else s.go_long()
    s.on_open_position(trade_id=1, setup=setup)
    return s, setup


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def test_unset_trail_pct_serializes_exactly_as_released():
    schema = get_node("exec.order_constructor").params_schema
    for params in (schema(), schema(trail_pct=None), schema(**_RELEASED_DEFAULTS)):
        assert params.trail_pct is None
        assert params.model_dump() == _RELEASED_DEFAULTS
        assert list(params.model_dump()) == list(_RELEASED_DEFAULTS)
        assert params.model_dump_json() == (
            '{"sl_pct":2.0,"risk_reward":2.0,"tp_pct":null,'
            '"risk_pct":1.0,"leverage":1.0,"entry_type":"market"}'
        )
        assert _digest(params.model_dump()) == _digest(_RELEASED_DEFAULTS)


def test_set_trail_pct_is_serialized_and_round_trips():
    schema = get_node("exec.order_constructor").params_schema
    params = schema(trail_pct=3)
    assert params.model_dump() == {**_RELEASED_DEFAULTS, "trail_pct": 3.0}
    assert schema(**params.model_dump()) == params
    assert _digest(params.model_dump()) != _digest(_RELEASED_DEFAULTS)


@pytest.mark.parametrize("value", [0, -1, 100, 150])
def test_trail_pct_must_be_between_zero_and_one_hundred_exclusive(value):
    schema = get_node("exec.order_constructor").params_schema
    with pytest.raises(ValidationError):
        schema(trail_pct=value)
    with pytest.raises(GraphValidationError):
        assemble_from_graph(_native({"trail_pct": value}))


def test_unset_trail_pct_order_and_evidence_are_unchanged():
    s = assemble_from_graph(_native({}))
    _bar(s, 100.0)
    setup = s.go_long()
    order = s._ensure_stepped().orders[0]
    assert order.metadata == {}
    node = next(n for n in setup.decision_context["nodes"] if n["node_id"] == "order")
    assert node["params"] == _RELEASED_DEFAULTS


def test_unset_trail_pct_never_moves_the_stop():
    s, _ = _open({})
    _bar(s, 150.0, 1)
    assert s.on_sl_update(trade_id=1) is None


def test_initial_stop_and_size_do_not_depend_on_trail_pct():
    _, plain = _open({"sl_pct": 2.0})
    _, trailed = _open({"sl_pct": 2.0, "trail_pct": 10.0})
    assert trailed.stop_loss == pytest.approx(98.0)
    assert (trailed.stop_loss, trailed.take_profit, trailed.size, trailed.entry_price) == (
        plain.stop_loss,
        plain.take_profit,
        plain.size,
        plain.entry_price,
    )


def test_long_stop_rises_with_the_close_and_never_falls():
    s, setup = _open({"sl_pct": 2.0, "trail_pct": 5.0})
    assert setup.stop_loss == pytest.approx(98.0)
    # 100 * 0.95 = 95 is below the initial stop: no move.
    assert s.on_sl_update(trade_id=1) is None
    _bar(s, 110.0, 1)
    assert s.on_sl_update(trade_id=1) == pytest.approx(104.5)
    # Same close again: the stop does not move, so no update is emitted.
    assert s.on_sl_update(trade_id=1) is None
    _bar(s, 105.0, 2)  # 99.75 would lower the stop
    assert s.on_sl_update(trade_id=1) is None
    _bar(s, 120.0, 3)
    assert s.on_sl_update(trade_id=1) == pytest.approx(114.0)


def test_short_stop_falls_with_the_close_and_never_rises():
    s, setup = _open({"sl_pct": 2.0, "trail_pct": 5.0}, short=True)
    assert setup.stop_loss == pytest.approx(102.0)
    assert s.on_sl_update(trade_id=1) is None
    _bar(s, 90.0, 1)
    assert s.on_sl_update(trade_id=1) == pytest.approx(94.5)
    _bar(s, 95.0, 2)
    assert s.on_sl_update(trade_id=1) is None


def test_trailing_stops_after_the_position_closes():
    s, _ = _open({"trail_pct": 5.0})
    s.on_close_position(trade_id=1, result={})
    _bar(s, 200.0, 1)
    assert s.on_sl_update(trade_id=1) is None


def test_trail_pct_is_recorded_in_decision_evidence():
    _, setup = _open({"trail_pct": 5.0})
    node = next(n for n in setup.decision_context["nodes"] if n["node_id"] == "order")
    assert node["params"]["trail_pct"] == 5.0


def test_legacy_dynamic_exit_together_with_trail_pct_is_rejected():
    dynamic_exit = extract_dynamic_exit(_legacy(trailing=True))
    assert dynamic_exit is not None
    with pytest.raises(GraphValidationError, match="trail_pct"):
        build_graph_strategy(_native({"trail_pct": 5.0}), dynamic_exit)


def test_legacy_graph_cannot_smuggle_trail_pct_through_exit_params():
    for trailing in (False, True):
        graph = _legacy(exit_params={"sl_pct": 2.0, "trail_pct": 5.0}, trailing=trailing)
        with pytest.raises(GraphValidationError):
            assemble_from_graph(graph)


def test_compile_legacy_keeps_the_dynamic_exit_on_the_seam():
    graph = _legacy(trailing=True)
    compiled = compile_legacy(graph)
    order = next(b for b in compiled["blocks"] if b["type"] == "exec.order_constructor")
    assert order["params"] == {
        "sl_pct": 2.0,
        "risk_reward": 2.0,
        "risk_pct": 1.0,
        "entry_type": "market",
    }
    assert all(not b["type"].startswith("exit.") for b in compiled["blocks"])
    assert extract_dynamic_exit(graph) is not None


def test_legacy_trailing_stop_graph_behaves_as_before():
    import numpy as np

    s = assemble_from_graph(_legacy(trailing=True))
    closes = [10, 9, 8, 7, 12]
    _bar(s, closes[-1], len(closes) - 1)
    s.closes = np.array(closes, float)
    s.highs = s.lows = s.opens = s.closes
    s.volumes = np.ones_like(s.closes)
    setup = s.go_long()
    s.on_open_position(trade_id=1, setup=setup)
    assert setup.stop_loss == pytest.approx(11.76)
    assert s.on_sl_update(trade_id=1) is None  # 12 * 0.95 = 11.4 < 11.76
    s.close = 14.0
    assert s.on_sl_update(trade_id=1) == pytest.approx(13.3)
    assert s.on_sl_update(trade_id=1) is None
