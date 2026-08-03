import koval.strategy.nodes  # noqa: F401
from koval.strategy.graph.entities import MarketEvent, PolicyDecision, TradingIntent
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.registry import get_node


def _ctx():
    return BarContext(
        close=1.0, high=1.0, low=1.0, open=1.0, volume=1.0, bar_index=0, timestamp_ms=0
    )


def _ev(direction):
    return MarketEvent(
        bar_index=0, timestamp_ms=0, source_node_id="s", kind="bos", direction=direction
    )


def test_confluence_and_agrees_bullish():
    spec = get_node("interp.confluence_and")
    ev = spec.factory(spec.params_schema())
    out = ev(_ctx(), {"events": [_ev("bullish"), _ev("bullish")]}, {})
    assert out["agreement"].direction == "bullish"


def test_confluence_and_disagreement_is_neutral():
    spec = get_node("interp.confluence_and")
    ev = spec.factory(spec.params_schema())
    out = ev(_ctx(), {"events": [_ev("bullish"), _ev("bearish")]}, {})
    assert out["agreement"].direction == "neutral"


def test_confluence_requires_all_wired_signals_to_fire():
    # Legacy linear-AND parity: with min_signals=2, a single fired signal is not
    # enough — every wired signal must have produced an event.
    spec = get_node("interp.confluence_and")
    ev = spec.factory(spec.params_schema(min_signals=2))
    assert ev(_ctx(), {"events": [_ev("bullish")]}, {})["agreement"].direction == "neutral"
    out = ev(_ctx(), {"events": [_ev("bullish"), _ev("bullish")]}, {})
    assert out["agreement"].direction == "bullish"


def test_direction_gate_emits_intent_when_allowed_and_policies_pass():
    spec = get_node("interp.direction_gate")
    ev = spec.factory(spec.params_schema(allow_long=True, allow_short=False))
    agreement = MarketEvent(
        bar_index=0, timestamp_ms=0, source_node_id="a", kind="agreement", direction="bullish"
    )
    policy_ok = PolicyDecision(bar_index=0, timestamp_ms=0, source_node_id="p", allowed=True)
    out = ev(_ctx(), {"agreement": [agreement], "policies": [policy_ok]}, {})
    assert isinstance(out["intent"], TradingIntent)
    assert out["intent"].side == "buy"


def test_direction_gate_blocks_when_policy_fails():
    spec = get_node("interp.direction_gate")
    ev = spec.factory(spec.params_schema(allow_long=True, allow_short=True))
    agreement = MarketEvent(
        bar_index=0, timestamp_ms=0, source_node_id="a", kind="agreement", direction="bullish"
    )
    policy_blocked = PolicyDecision(bar_index=0, timestamp_ms=0, source_node_id="p", allowed=False)
    out = ev(_ctx(), {"agreement": [agreement], "policies": [policy_blocked]}, {})
    assert out["intent"] is None
