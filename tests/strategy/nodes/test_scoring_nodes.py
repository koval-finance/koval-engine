import pickle

import koval.strategy.nodes  # noqa: F401
from koval.strategy.graph.entities import (
    MarketState,
    ScoredSetup,
    SetupCandidate,
    TradingIntent,
)
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.registry import get_node


def _ctx(i: int = 7) -> BarContext:
    return BarContext(
        close=1.0, high=1.0, low=1.0, open=1.0, volume=1.0, bar_index=i, timestamp_ms=i * 1000
    )


def _cand(direction="bullish", kinds=("order_block", "choch")) -> SetupCandidate:
    return SetupCandidate(
        bar_index=7,
        timestamp_ms=7000,
        source_node_id="setup_generator",
        setup_id="s1",
        setup_type="ob_reversal",
        direction=direction,
        window=(3, 7),
        metadata={"event_kinds": list(kinds)},
    )


def _node(node_type, **params):
    spec = get_node(node_type)
    return spec.factory(spec.params_schema(**params))


# ---------------------------------------------------------------------------
# setup_score
# ---------------------------------------------------------------------------


def test_setup_score_sums_event_points_and_base():
    ev = _node("interp.setup_score", points={"order_block": 20, "choch": 25}, base=5)
    out = ev(_ctx(), {"candidate": [_cand()]}, {})
    scored = out["scored"]
    assert isinstance(scored, ScoredSetup)
    assert scored.setup_quality_score == 50
    assert scored.setup_id == "s1"
    assert scored.reasoning_chain == "Setup 50 (order_block +20, choch +25)"


def test_setup_score_zero_points_renders_bare():
    ev = _node("interp.setup_score", points={})
    out = ev(_ctx(), {"candidate": [_cand()]}, {})
    assert out["scored"].setup_quality_score == 0
    assert out["scored"].reasoning_chain == "Setup 0"


def test_setup_score_no_candidate_returns_none():
    ev = _node("interp.setup_score", points={})
    assert ev(_ctx(), {"candidate": []}, {})["scored"] is None


def test_scored_setup_is_picklable():
    ev = _node("interp.setup_score", points={"order_block": 20})
    scored = ev(_ctx(), {"candidate": [_cand()]}, {})["scored"]
    assert pickle.loads(pickle.dumps(scored)) == scored


# ---------------------------------------------------------------------------
# context_score
# ---------------------------------------------------------------------------


def _state(kind, status) -> MarketState:
    return MarketState(
        bar_index=7, timestamp_ms=7000, source_node_id=kind, kind=kind, status=status
    )


def test_context_score_rewards_trend_alignment():
    ev = _node("interp.context_score", points={"trend_aligned": 20, "volatility_expansion": 5})
    states = [_state("trend_bias", "bullish"), _state("volatility_regime", "expansion")]
    out = ev(_ctx(), {"candidate": [_cand(direction="bullish")], "states": states}, {})
    assert out["scored"].context_score == 25
    assert (
        out["scored"].reasoning_chain == "Context 25 (trend_aligned +20, volatility_expansion +5)"
    )


def test_context_score_penalises_counter_trend():
    ev = _node("interp.context_score", points={"counter_trend": -10})
    states = [_state("trend_bias", "bearish")]
    out = ev(_ctx(), {"candidate": [_cand(direction="bullish")], "states": states}, {})
    assert out["scored"].context_score == -10


def test_context_score_neutral_when_no_states():
    ev = _node("interp.context_score", points={"trend_aligned": 20})
    out = ev(_ctx(), {"candidate": [_cand()], "states": []}, {})
    assert out["scored"].context_score == 0
    assert out["scored"].reasoning_chain == "Context 0"


def test_context_score_no_candidate_returns_none():
    ev = _node("interp.context_score", points={})
    assert ev(_ctx(), {"candidate": [], "states": []}, {})["scored"] is None


def test_context_score_dedups_duplicate_state_kinds():
    # two trend_bias states on the fan-in port must not double-count
    ev = _node("interp.context_score", points={"trend_aligned": 20})
    states = [_state("trend_bias", "bullish"), _state("trend_bias", "bullish")]
    out = ev(_ctx(), {"candidate": [_cand(direction="bullish")], "states": states}, {})
    assert out["scored"].context_score == 20


# ---------------------------------------------------------------------------
# qualification_gate
# ---------------------------------------------------------------------------


def _scored(setup_id="s1", sq=0, cs=0, reason="") -> ScoredSetup:
    return ScoredSetup(
        bar_index=7,
        timestamp_ms=7000,
        source_node_id="x",
        setup_id=setup_id,
        setup_quality_score=sq,
        context_score=cs,
        reasoning_chain=reason,
    )


def test_gate_passes_at_threshold_boundary():
    ev = _node("interp.qualification_gate", qualification_threshold=45)
    sq = _scored(sq=20, reason="Setup 20 (order_block +20)")
    cs = _scored(cs=25, reason="Context 25 (trend_aligned +25)")
    out = ev(_ctx(), {"setup_score": [sq], "context_score": [cs]}, {})
    scored = out["scored"]
    assert scored.final_score == 45
    assert scored.qualified is True
    assert scored.reasoning_chain == (
        "Setup 20 (order_block +20); Context 25 (trend_aligned +25); Gate PASS 45>=45"
    )


def test_gate_fails_below_threshold():
    ev = _node("interp.qualification_gate", qualification_threshold=50)
    out = ev(_ctx(), {"setup_score": [_scored(sq=20)], "context_score": [_scored(cs=25)]}, {})
    assert out["scored"].qualified is False
    assert "Gate FAIL 45<50" in out["scored"].reasoning_chain


def test_gate_works_with_only_setup_score_wired():
    ev = _node("interp.qualification_gate", qualification_threshold=10)
    out = ev(_ctx(), {"setup_score": [_scored(sq=20)], "context_score": []}, {})
    assert out["scored"].final_score == 20
    assert out["scored"].qualified is True


def test_gate_no_inputs_returns_none():
    ev = _node("interp.qualification_gate")
    assert ev(_ctx(), {"setup_score": [], "context_score": []}, {})["scored"] is None


def test_gate_does_not_combine_scores_from_different_setups():
    ev = _node("interp.qualification_gate", qualification_threshold=40)
    setup_a = _scored(setup_id="a", sq=20, reason="Setup A")
    context_b = _scored(setup_id="b", cs=25, reason="Context B")

    out = ev(_ctx(), {"setup_score": [setup_a], "context_score": [context_b]}, {})

    assert out["scored"] is None


def test_gate_selects_context_score_with_matching_setup_id():
    ev = _node("interp.qualification_gate", qualification_threshold=40)
    setup_a = _scored(setup_id="a", sq=20, reason="Setup A")
    context_b = _scored(setup_id="b", cs=99, reason="Context B")
    context_a = _scored(setup_id="a", cs=25, reason="Context A")

    out = ev(
        _ctx(),
        {"setup_score": [setup_a], "context_score": [context_b, context_a]},
        {},
    )

    assert out["scored"].setup_id == "a"
    assert out["scored"].final_score == 45


# ---------------------------------------------------------------------------
# signal_emitter
# ---------------------------------------------------------------------------


def _qualified(direction_ok=True, reason="Gate PASS 45>=40") -> ScoredSetup:
    return ScoredSetup(
        bar_index=7,
        timestamp_ms=7000,
        source_node_id="qualification_gate",
        setup_id="s1",
        final_score=45,
        qualified=direction_ok,
        reasoning_chain=reason,
    )


def test_signal_emitter_emits_buy_for_bullish_qualified():
    ev = _node("interp.signal_emitter")
    out = ev(
        _ctx(),
        {"scored": [_qualified()], "candidate": [_cand(direction="bullish")]},
        {},
    )
    intent = out["intent"]
    assert isinstance(intent, TradingIntent)
    assert intent.side == "buy"
    assert intent.setup_id == "s1"
    assert intent.metadata["reasoning_chain"] == "Gate PASS 45>=40"
    assert intent.metadata["final_score"] == 45


def test_signal_emitter_emits_sell_for_bearish():
    ev = _node("interp.signal_emitter")
    out = ev(_ctx(), {"scored": [_qualified()], "candidate": [_cand(direction="bearish")]}, {})
    assert out["intent"].side == "sell"


def test_signal_emitter_silent_when_not_qualified():
    ev = _node("interp.signal_emitter")
    out = ev(
        _ctx(),
        {"scored": [_qualified(direction_ok=False)], "candidate": [_cand()]},
        {},
    )
    assert out["intent"] is None


def test_signal_emitter_silent_for_neutral_direction():
    ev = _node("interp.signal_emitter")
    out = ev(_ctx(), {"scored": [_qualified()], "candidate": [_cand(direction="neutral")]}, {})
    assert out["intent"] is None


def test_signal_emitter_silent_without_candidate():
    ev = _node("interp.signal_emitter")
    assert ev(_ctx(), {"scored": [_qualified()], "candidate": []}, {})["intent"] is None


def test_signal_emitter_does_not_mix_candidate_from_different_setup():
    ev = _node("interp.signal_emitter")
    scored = _qualified().model_copy(update={"setup_id": "a"})
    candidate_b = _cand(direction="bearish").model_copy(update={"setup_id": "b"})

    out = ev(_ctx(), {"scored": [scored], "candidate": [candidate_b]}, {})

    assert out["intent"] is None


def test_signal_emitter_selects_candidate_with_matching_setup_id():
    ev = _node("interp.signal_emitter")
    scored = _qualified().model_copy(update={"setup_id": "a"})
    candidate_b = _cand(direction="bearish").model_copy(update={"setup_id": "b"})
    candidate_a = _cand(direction="bullish").model_copy(update={"setup_id": "a"})

    out = ev(
        _ctx(),
        {"scored": [scored], "candidate": [candidate_b, candidate_a]},
        {},
    )

    assert out["intent"].setup_id == "a"
    assert out["intent"].side == "buy"
