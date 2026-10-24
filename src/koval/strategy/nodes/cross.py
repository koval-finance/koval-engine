"""Versioned bounded crossing state. A down event is data, not an entry command."""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict

from koval.exchanges.base import timeframe_ms
from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import MarketEvent, NumericValueV1
from koval.strategy.graph.evidence import observe
from koval.strategy.graph.node import NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.nodes.value_contracts import first, scalar_binding, scalar_status

CROSS_STATE_VERSION = 1


class CrossParamsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    direction: Literal["up", "down", "both"] = "both"


def cross_direction(
    previous_left: float, previous_right: float, left: float, right: float
) -> str | None:
    """Equality arms a subsequent strict transition; no epsilon or sign conversion."""
    if previous_left <= previous_right and left > right:
        return "up"
    if previous_left >= previous_right and left < right:
        return "down"
    return None


def _cross_factory(p):
    def evaluate(ctx, inputs, state):
        if state and state.get("version") != CROSS_STATE_VERSION:
            raise ValueError("unsupported cross state version")
        left, right = first(inputs, "left"), first(inputs, "right")
        readiness, reason = scalar_status(ctx, [left, right])
        previous = state.get("previous")
        last = state.get("last_timestamp_ms")
        if last is not None and ctx.timestamp_ms < last:
            raise ValueError("cross cursor moved backwards")
        # A repeated decision cannot replay an event or revise already consumed state.
        if last == ctx.timestamp_ms:
            observe(
                ctx,
                values={"readiness": readiness, "reason": "duplicate_cursor"},
                predicate="consecutive ready pairs",
                result="recorded",
            )
            return {"event": None}
        event, direction = None, None
        current = None
        if readiness == "ready":
            current = {"left": left.model_dump(), "right": right.model_dump()}
            if (
                previous is not None
                and ctx.timestamp_ms == last + timeframe_ms(ctx.timeframe)
                and all(
                    scalar_binding(previous[k]) == scalar_binding(current[k])
                    for k in ("left", "right")
                )
            ):
                direction = cross_direction(
                    previous["left"]["value"], previous["right"]["value"], left.value, right.value
                )
            if direction and p.direction in {direction, "both"}:
                event = MarketEvent(
                    bar_index=ctx.bar_index,
                    timestamp_ms=ctx.timestamp_ms,
                    source_node_id="fact.cross",
                    kind="cross",
                    direction="bullish" if direction == "up" else "bearish",
                    price_levels=[right.value] if right.unit == "price_quote_per_base" else [],
                    metadata=deepcopy(
                        {
                            "direction": direction,
                            "formula_version": "cross_v1",
                            "state_version": CROSS_STATE_VERSION,
                            "left": current["left"],
                            "right": current["right"],
                            "previous": previous,
                        }
                    ),
                )
        state.update(
            version=CROSS_STATE_VERSION, last_timestamp_ms=ctx.timestamp_ms, previous=current
        )
        observe(
            ctx,
            values={
                "readiness": readiness,
                "reason": reason,
                "direction": direction,
                "left": None if left is None else left.model_dump(),
                "right": None if right is None else right.model_dump(),
            },
            predicate="previous_left <= previous_right and left > right (up); reverse for down",
            result="unavailable" if readiness != "ready" else "passed" if event else "failed",
        )
        return {"event": event}

    return evaluate


register_node(
    NodeSpec(
        "fact.cross",
        Domain.FACT,
        "Cross Values",
        "One event on a consecutive ready strict crossing.",
        CrossParamsV1,
        {k: PortSpec(k, (NumericValueV1,)) for k in ("left", "right")},
        {"event": PortSpec("event", (MarketEvent,), required=False)},
        _cross_factory,
    )
)
