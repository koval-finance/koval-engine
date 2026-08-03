"""Scoring + decision interpretation nodes.

setup_score / context_score emit partial ScoredSetup scores (additive points
from node params, via the pure scoring helper); qualification_gate combines them
against a threshold; signal_emitter turns a qualified setup into a TradingIntent
carrying the reasoning chain. All INTERPRETATION domain.
"""

from __future__ import annotations

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import (
    MarketState,
    ScoredSetup,
    SetupCandidate,
    TradingIntent,
)
from koval.strategy.graph.node import NodeEvaluate, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.helpers.interp.scoring import score_additive
from koval.strategy.schemas import _StrictModel

_SIDE = {"bullish": "buy", "bearish": "sell"}


# ---------------------------------------------------------------------------
# setup_score
# ---------------------------------------------------------------------------


class SetupScoreParams(_StrictModel):
    points: dict[str, int] = {}
    base: int = 0


def _setup_score_factory(p: SetupScoreParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        cands = inputs.get("candidate", [])
        if not cands:
            return {"scored": None}
        c = cands[0]
        total, breakdown = score_additive(p.points, c.metadata.get("event_kinds", []))
        total += p.base
        reasoning = f"Setup {total}" + (f" ({breakdown})" if breakdown else "")
        return {
            "scored": ScoredSetup(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="setup_score",
                setup_id=c.setup_id,
                setup_quality_score=total,
                reasoning_chain=reasoning,
            )
        }

    return evaluate


# ---------------------------------------------------------------------------
# context_score
# ---------------------------------------------------------------------------


class ContextScoreParams(_StrictModel):
    points: dict[str, int] = {}


def _context_keys(direction: str, states: list[MarketState]) -> list[str]:
    # One key per STATE dimension (last state per kind wins), so duplicate
    # states of the same kind on the fan-in port never double-count.
    by_kind: dict[str, MarketState] = {}
    for s in states:
        by_kind[s.kind] = s
    keys: list[str] = []
    for kind, s in by_kind.items():
        if kind == "trend_bias":
            if direction == "neutral" or s.status == "neutral":
                keys.append("trend_neutral")
            elif s.status == direction:
                keys.append("trend_aligned")
            else:
                keys.append("counter_trend")
        elif kind == "volatility_regime":
            if s.status == "expansion":
                keys.append("volatility_expansion")
            elif s.status == "compression":
                keys.append("volatility_compression")
    return keys


def _context_score_factory(p: ContextScoreParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        cands = inputs.get("candidate", [])
        if not cands:
            return {"scored": None}
        c = cands[0]
        keys = _context_keys(c.direction, inputs.get("states", []))
        total, breakdown = score_additive(p.points, keys)
        reasoning = f"Context {total}" + (f" ({breakdown})" if breakdown else "")
        return {
            "scored": ScoredSetup(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="context_score",
                setup_id=c.setup_id,
                context_score=total,
                reasoning_chain=reasoning,
            )
        }

    return evaluate


# ---------------------------------------------------------------------------
# qualification_gate
# ---------------------------------------------------------------------------


class QualificationGateParams(_StrictModel):
    qualification_threshold: int = 0


def _qualification_gate_factory(p: QualificationGateParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        sq_in = inputs.get("setup_score", [])
        cs_in = inputs.get("context_score", [])
        if not sq_in:
            return {"scored": None}
        setup_score = sq_in[0]
        context_score = next(
            (score for score in cs_in if score.setup_id == setup_score.setup_id),
            None,
        )
        if cs_in and context_score is None:
            return {"scored": None}
        sq = setup_score.setup_quality_score
        cs = context_score.context_score if context_score is not None else 0
        setup_id = setup_score.setup_id
        sq_reason = setup_score.reasoning_chain
        cs_reason = context_score.reasoning_chain if context_score is not None else "Context 0"
        final = sq + cs
        qualified = final >= p.qualification_threshold
        verdict = "PASS" if qualified else "FAIL"
        op = ">=" if qualified else "<"
        reasoning = (
            f"{sq_reason}; {cs_reason}; Gate {verdict} {final}{op}{p.qualification_threshold}"
        )
        return {
            "scored": ScoredSetup(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="qualification_gate",
                setup_id=setup_id,
                setup_quality_score=sq,
                context_score=cs,
                final_score=final,
                qualified=qualified,
                reasoning_chain=reasoning,
            )
        }

    return evaluate


# ---------------------------------------------------------------------------
# signal_emitter
# ---------------------------------------------------------------------------


class SignalEmitterParams(_StrictModel):
    entry_model_hint: str | None = None


def _signal_emitter_factory(p: SignalEmitterParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        scored_in = inputs.get("scored", [])
        cand_in = inputs.get("candidate", [])
        if not scored_in or not cand_in:
            return {"intent": None}
        scored = scored_in[0]
        cand = next(
            (candidate for candidate in cand_in if candidate.setup_id == scored.setup_id),
            None,
        )
        if cand is None:
            return {"intent": None}
        if not scored.qualified:
            return {"intent": None}
        side = _SIDE.get(cand.direction)
        if side is None:
            return {"intent": None}
        return {
            "intent": TradingIntent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="signal_emitter",
                side=side,
                setup_id=scored.setup_id,
                entry_model_hint=p.entry_model_hint,
                metadata={
                    "reasoning_chain": scored.reasoning_chain,
                    "final_score": scored.final_score,
                },
            )
        }

    return evaluate


def _register() -> None:
    register_node(
        NodeSpec(
            "interp.setup_score",
            Domain.INTERPRETATION,
            "Setup Score",
            "Additive quality points from the setup's contributing event kinds.",
            SetupScoreParams,
            {"candidate": PortSpec("candidate", (SetupCandidate,))},
            {"scored": PortSpec("scored", (ScoredSetup,))},
            _setup_score_factory,
        )
    )
    register_node(
        NodeSpec(
            "interp.context_score",
            Domain.INTERPRETATION,
            "Context Score",
            "Additive context points from market STATE (trend / volatility regime).",
            ContextScoreParams,
            {
                "candidate": PortSpec("candidate", (SetupCandidate,)),
                "states": PortSpec(
                    "states",
                    (MarketState,),
                    required=False,
                    max_connections=None,
                ),
            },
            {"scored": PortSpec("scored", (ScoredSetup,))},
            _context_score_factory,
        )
    )
    register_node(
        NodeSpec(
            "interp.qualification_gate",
            Domain.INTERPRETATION,
            "Qualification Gate",
            "Combine setup + context scores; qualify against a threshold.",
            QualificationGateParams,
            {
                "setup_score": PortSpec("setup_score", (ScoredSetup,)),
                "context_score": PortSpec("context_score", (ScoredSetup,), required=False),
            },
            {"scored": PortSpec("scored", (ScoredSetup,))},
            _qualification_gate_factory,
        )
    )
    register_node(
        NodeSpec(
            "interp.signal_emitter",
            Domain.INTERPRETATION,
            "Signal Emitter",
            "Emit a TradingIntent for a qualified setup, carrying its reasoning chain.",
            SignalEmitterParams,
            {
                "scored": PortSpec("scored", (ScoredSetup,)),
                "candidate": PortSpec("candidate", (SetupCandidate,)),
            },
            {"intent": PortSpec("intent", (TradingIntent,))},
            _signal_emitter_factory,
        )
    )


_register()
