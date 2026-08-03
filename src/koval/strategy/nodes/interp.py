"""INTERPRETATION nodes for the compatibility path.

confluence_and: every wired signal must fire (>= min_signals events) and share
a direction — the legacy linear-AND "all signals agree" rule.
direction_gate: emit a TradingIntent iff the agreed direction is allowed and
every incoming policy is allowed. Together they reproduce the legacy linear-AND.
Richer interpretation lives in ``aggregator.py`` and ``scoring.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import MarketEvent, PolicyDecision, TradingIntent
from koval.strategy.graph.node import NodeEvaluate, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.schemas import _StrictModel


class _ConfluenceParams(_StrictModel):
    # Legacy linear-AND required EVERY wired signal to fire and agree. A signal
    # that does not fire emits no event, so we additionally require at least
    # ``min_signals`` events present (compat sets this to the signal count).
    min_signals: int = Field(1, ge=0)


class _DirectionGateParams(_StrictModel):
    allow_long: bool = True
    allow_short: bool = True


def _confluence_factory(p: _ConfluenceParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        events = inputs.get("events", [])
        directions = {e.direction for e in events if e.direction}
        agreed: Literal["bullish", "bearish", "neutral"]
        if len(events) >= p.min_signals and len(directions) == 1 and "neutral" not in directions:
            agreed = directions.pop()
        else:
            agreed = "neutral"
        return {
            "agreement": MarketEvent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="confluence",
                kind="agreement",
                direction=agreed,
            )
        }

    return evaluate


def _direction_gate_factory(p: _DirectionGateParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        agreement = inputs.get("agreement", [])
        policies = inputs.get("policies", [])
        if not agreement:
            return {"intent": None}
        direction = agreement[0].direction
        if any(not pol.allowed for pol in policies):
            return {"intent": None}
        if direction == "bullish" and p.allow_long:
            side = "buy"
        elif direction == "bearish" and p.allow_short:
            side = "sell"
        else:
            return {"intent": None}
        return {
            "intent": TradingIntent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="direction_gate",
                side=side,
            )
        }

    return evaluate


def _register() -> None:
    register_node(
        NodeSpec(
            "interp.confluence_and",
            Domain.INTERPRETATION,
            "Confluence (AND)",
            "All incoming signal events must agree on direction.",
            _ConfluenceParams,
            {"events": PortSpec("events", (MarketEvent,), max_connections=None)},
            {"agreement": PortSpec("agreement", (MarketEvent,))},
            _confluence_factory,
        )
    )
    register_node(
        NodeSpec(
            "interp.direction_gate",
            Domain.INTERPRETATION,
            "Direction Gate",
            "Emit a TradingIntent if the agreed direction is allowed and all policies pass.",
            _DirectionGateParams,
            {
                "agreement": PortSpec("agreement", (MarketEvent,)),
                "policies": PortSpec(
                    "policies",
                    (PolicyDecision,),
                    required=False,
                    max_connections=None,
                ),
            },
            {"intent": PortSpec("intent", (TradingIntent,))},
            _direction_gate_factory,
        )
    )


_register()
