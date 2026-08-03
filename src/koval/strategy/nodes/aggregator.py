"""Temporal interpretation nodes.

event_aggregator buffers time-separated FACT events into setups across bars
(the engine's only multi-bar interpretation state); setup_generator types and
direction-resolves the assembled chain. Both live in the INTERPRETATION domain.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import MarketEvent, SetupCandidate
from koval.strategy.graph.node import NodeEvaluate, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.schemas import _StrictModel

_INVERT = {"bullish": "bearish", "bearish": "bullish", "neutral": "neutral"}


# ---------------------------------------------------------------------------
# event_aggregator
# ---------------------------------------------------------------------------


class EventAggregatorParams(_StrictModel):
    event_sequence: list[str] = []
    timeout_bars: int = Field(20, ge=1)
    order_policy: Literal["strict", "any"] = "strict"
    direction_policy: Literal["consistent", "ignore"] = "consistent"
    max_open_chains: int = Field(16, ge=1)


def _can_take(chain: dict, kind: str, direction: str | None, p: EventAggregatorParams) -> bool:
    if p.order_policy == "strict":
        if not chain["remaining"] or chain["remaining"][0] != kind:
            return False
    else:  # any
        if kind not in chain["remaining"]:
            return False
    if p.direction_policy == "consistent" and chain["direction"] is not None:
        return direction == chain["direction"]
    return True


def _event_aggregator_factory(p: EventAggregatorParams) -> NodeEvaluate:
    seq = p.event_sequence

    def evaluate(ctx, inputs, state):
        if not seq:
            return {"candidate": None}
        chains: list[dict] = state.setdefault("chains", [])
        # 1. evict expired partial chains
        chains[:] = [c for c in chains if c["deadline_bar"] >= ctx.bar_index]

        for ev in inputs.get("events", []):
            kind, direction = ev.kind, ev.direction
            took = False
            for chain in chains:  # oldest first
                if _can_take(chain, kind, direction, p):
                    chain["matched"].append([ev.source_node_id, ev.bar_index, kind, direction])
                    chain["remaining"].remove(kind)
                    if p.direction_policy == "consistent" and chain["direction"] is None:
                        chain["direction"] = direction
                    took = True
                    break
            can_seed = kind == seq[0] if p.order_policy == "strict" else kind in seq
            if not took and can_seed:
                if p.direction_policy == "consistent" and direction not in ("bullish", "bearish"):
                    continue
                remaining = list(seq)
                remaining.remove(kind)
                chains.append(
                    {
                        "matched": [[ev.source_node_id, ev.bar_index, kind, direction]],
                        "remaining": remaining,
                        "deadline_bar": ctx.bar_index + p.timeout_bars,
                        "direction": direction if p.direction_policy == "consistent" else None,
                    }
                )

        complete = [c for c in chains if not c["remaining"]]
        partials = [c for c in chains if c["remaining"]]
        # Cap concurrent partial chains (pickle-size guard), keeping the most
        # recent. Applied after the event loop and to partials only, so a chain
        # that completed this bar is never evicted before it is emitted.
        if len(partials) > p.max_open_chains:
            partials = partials[-p.max_open_chains :]
        chains[:] = partials
        if not complete:
            return {"candidate": None}
        complete.sort(key=lambda c: (c["matched"][0][1], c["matched"][0][0]))
        chosen = complete[0]
        matched = chosen["matched"]
        kinds = [m[2] for m in matched]
        first_bar, last_bar = matched[0][1], matched[-1][1]
        # Per-run monotonic suffix so setup_ids are unique even when two chains
        # share the same first node + window (collision-free provenance).
        state["minted"] = state.get("minted", 0) + 1
        return {
            "candidate": SetupCandidate(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="event_aggregator",
                setup_id=f"{matched[0][0]}@{first_bar}-{last_bar}#{state['minted']}",
                setup_type="->".join(kinds),
                # direction_policy="ignore" leaves chain direction None -> a
                # neutral setup; signal_emitter emits no side for neutral, so
                # ignore-mode is analysis-only unless a custom emitter is wired.
                direction=chosen["direction"] or "neutral",
                contributing_events=[(m[0], m[1]) for m in matched],
                window=(first_bar, last_bar),
                metadata={
                    # event_kinds drives setup_score; event_strengths is a
                    # forward hook for strength-weighted scoring (unused in v1).
                    "event_kinds": kinds,
                    "event_strengths": [None for _ in matched],
                },
            )
        }

    return evaluate


# ---------------------------------------------------------------------------
# setup_generator
# ---------------------------------------------------------------------------


class SetupGeneratorParams(_StrictModel):
    setup_type_map: dict[str, str] = {}
    direction_mode: Literal["keep", "invert"] = "keep"


def _setup_generator_factory(p: SetupGeneratorParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        cands = inputs.get("candidate", [])
        if not cands:
            return {"candidate": None}
        c = cands[0]
        new_type = p.setup_type_map.get(c.setup_type, c.setup_type)
        direction = _INVERT[c.direction] if p.direction_mode == "invert" else c.direction
        return {
            "candidate": c.model_copy(
                update={
                    "setup_type": new_type,
                    "direction": direction,
                    "source_node_id": "setup_generator",
                }
            )
        }

    return evaluate


def _register() -> None:
    register_node(
        NodeSpec(
            "interp.event_aggregator",
            Domain.INTERPRETATION,
            "Event Aggregator",
            "Assemble time-separated FACT events into a setup chain across bars.",
            EventAggregatorParams,
            {"events": PortSpec("events", (MarketEvent,), max_connections=None)},
            {"candidate": PortSpec("candidate", (SetupCandidate,))},
            _event_aggregator_factory,
        )
    )
    register_node(
        NodeSpec(
            "interp.setup_generator",
            Domain.INTERPRETATION,
            "Setup Generator",
            "Map an assembled chain to a typed setup and resolve trade direction.",
            SetupGeneratorParams,
            {"candidate": PortSpec("candidate", (SetupCandidate,))},
            {"candidate": PortSpec("candidate", (SetupCandidate,))},
            _setup_generator_factory,
        )
    )


_register()
