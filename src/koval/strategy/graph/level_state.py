"""Strict post-bar frozen-level checkpoints, independent of host persistence."""

from __future__ import annotations

import json
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from koval.exchanges.base import timeframe_ms
from koval.strategy.graph.entities import LevelSnapshotV1, NumericValueV1
from koval.strategy.graph.level_identity import (
    canonical_hash,
    frozen_level_id,
    frozen_source_hash,
    opportunity_id,
    primary_context_hash,
)


class LevelSetupParamsV1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"mode": {"const": "breakout_retest"}}},
                    "then": {
                        "required": ["tolerance_pct"],
                        "properties": {"tolerance_pct": {"type": "number"}},
                    },
                    "else": {"not": {"required": ["tolerance_pct"]}},
                }
            ]
        },
    )
    mode: Literal["sweep_reclaim", "breakout_retest", "breakout"]
    expires_after_bars: int = Field(ge=1, le=400)
    invalidation_buffer_pct: float = Field(ge=0, lt=100, allow_inf_nan=False)
    tolerance_pct: float | None = Field(default=None, ge=0, lt=100, allow_inf_nan=False)
    rearm: Literal["new_trigger_after_terminal"] = "new_trigger_after_terminal"

    @model_validator(mode="after")
    def _variant(self):
        if self.mode == "breakout_retest":
            if self.tolerance_pct is None:
                raise ValueError("breakout_retest requires tolerance_pct")
        elif "tolerance_pct" in self.model_fields_set:
            raise ValueError("tolerance_pct is only supported for breakout_retest")
        return self

    @model_serializer(mode="wrap")
    def _omit_unused_tolerance(self, handler):
        data = handler(self)
        if self.tolerance_pct is None:
            data.pop("tolerance_pct", None)
        return data

    def semantic_config(self):
        return self.model_dump(exclude_none=True)


class LevelArmingObservationV1(BaseModel):
    """A retained recorder observation must remain safe to select after restore."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    node_id: str = Field(min_length=1)
    runtime_node_id: str = Field(min_length=1)
    source_node_ids: list[str]
    node_type: str = Field(min_length=1)
    name: str
    params: dict
    timeframe: str | None
    timestamp_ms: int = Field(ge=0)
    values: dict
    predicate: str | None
    result: str = Field(min_length=1)
    role: str = Field(min_length=1)
    contributing_nodes: list[str]
    outputs: dict


class LevelSetupStateV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    integrity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: int = Field(ge=1, le=1)
    formula_version: Literal["level_setup_v1"]
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_id: str = Field(min_length=1)
    graph_binding_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str
    timeframe: str | None
    last_timestamp_ms: int = Field(ge=0)
    last_bar_index: int = Field(ge=0)
    last_decision_timestamp_ms: int | None = Field(ge=0)
    last_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    last_close: float | None = Field(gt=0, allow_inf_nan=False)
    last_primary_valid: bool
    last_trigger_key: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    last_trigger_timestamp_ms: int | None = Field(ge=0)
    last_trigger_bar: int | None = Field(ge=0)
    arming_evidence: list[LevelArmingObservationV1]
    arming_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output: LevelSnapshotV1

    @model_validator(mode="after")
    def _consistent(self):
        data = self.model_dump()
        digest = data.pop("integrity_hash")
        if canonical_hash(data) != digest:
            raise ValueError("level checkpoint integrity mismatch")
        o = self.output
        if canonical_hash(data["arming_evidence"]) != self.arming_evidence_hash:
            raise ValueError("level state arming evidence hash mismatch")
        if any(row.timestamp_ms > self.last_timestamp_ms for row in self.arming_evidence):
            raise ValueError("level state future arming evidence")
        if (
            o.source_node_id != self.node_id
            or o.config_hash != self.config_hash
            or o.symbol != self.symbol
            or o.timeframe != self.timeframe
            or o.timestamp_ms != self.last_timestamp_ms
            or o.bar_index != self.last_bar_index
        ):
            raise ValueError("level state output/cursor/identity mismatch")
        if self.last_primary_valid != (self.last_close is not None):
            raise ValueError("level state primary-close mismatch")
        trigger = (self.last_trigger_key, self.last_trigger_timestamp_ms, self.last_trigger_bar)
        if any(v is None for v in trigger) != all(v is None for v in trigger):
            raise ValueError("level state trigger watermark incomplete")
        if self.last_trigger_timestamp_ms is not None and (
            self.last_trigger_timestamp_ms > self.last_timestamp_ms
            or self.last_trigger_bar > self.last_bar_index
        ):
            raise ValueError("level state trigger watermark is in the future")
        if o.status == "unavailable":
            return self
        if o.source_hash != frozen_source_hash(
            o.lower_source, o.upper_source
        ) or o.level_id != frozen_level_id(self.node_id, o.trigger_key, o.source_hash):
            raise ValueError("level state frozen identity/hash mismatch")
        if (
            self.last_trigger_timestamp_ms is None
            or self.last_trigger_timestamp_ms < o.trigger_timestamp_ms
            or self.last_trigger_bar < o.armed_bar
        ):
            raise ValueError("level state missing arming trigger watermark")
        step = timeframe_ms(o.timeframe)
        if (
            o.detected_at_ms < o.trigger_timestamp_ms + step
            or o.detected_at_ms >= o.trigger_timestamp_ms + 2 * step
        ):
            raise ValueError("level state invalid detection clock")
        if o.status == "armed" and o.bar_index > o.expires_bar:
            raise ValueError("level state armed beyond expiry")
        if o.status == "consumed":
            cursor = json.dumps(
                [o.terminal_bar, o.terminal_timestamp_ms, o.consumed_at_ms], separators=(",", ":")
            )
            if (
                o.consumption_cursor != cursor
                or o.event_id != opportunity_id(o.level_id, cursor)
                or o.terminal_bar > o.expires_bar
                or o.reason != "opportunity_consumed"
                or not o.terminal_timestamp_ms + step
                <= o.consumed_at_ms
                < o.terminal_timestamp_ms + 2 * step
            ):
                raise ValueError("level state consumed event/cursor mismatch")
        if o.status == "expired" and (
            o.terminal_bar <= o.expires_bar or o.reason != "expiry_elapsed"
        ):
            raise ValueError("level state expiry mismatch")
        return self


def validate_level_setup_state(
    params, state, *, node_id="state.level_setup", graph_binding_hash=None
) -> None:
    """Reject a malformed/rebound snapshot before the caller mutates any state."""
    if not isinstance(state, dict):
        raise ValueError("level state must be an object")
    if not state:
        return
    saved = LevelSetupStateV1.model_validate(state)
    if (
        saved.node_id != node_id
        or saved.config_hash != canonical_hash(params.semantic_config())
        or saved.graph_binding_hash != graph_binding_hash
    ):
        raise ValueError("level state node/config identity mismatch")
    o = saved.output
    if o.status != "unavailable":
        if o.expires_bar != o.armed_bar + params.expires_after_bars:
            raise ValueError("level state expiry config mismatch")
        if params.mode == "breakout_retest" and o.upper_source is not None:
            raise ValueError("breakout_retest state requires a single level")
        if params.mode == "breakout_retest" and not isfinite(
            o.lower * (1 + params.tolerance_pct / 100)
        ):
            raise ValueError("level state nonfinite retest threshold")


def level_context_duplicate(ctx, state: dict) -> bool:
    """Preflight graph/strategy caches before any upstream evaluator mutates state."""
    if not state:
        return False
    if (ctx.symbol, ctx.timeframe) != (state["symbol"], state["timeframe"]):
        raise ValueError("level primary identity mismatch")
    if type(ctx.bar_index) is not int or type(ctx.timestamp_ms) is not int:
        raise ValueError("level requires integer primary cursors")
    if ctx.timestamp_ms == state["last_timestamp_ms"] and ctx.bar_index == state["last_bar_index"]:
        if primary_context_hash(ctx) != state["context_hash"]:
            raise ValueError("level duplicate primary context conflict")
        return True
    if ctx.timestamp_ms <= state["last_timestamp_ms"] or ctx.bar_index <= state["last_bar_index"]:
        raise ValueError("level primary cursor moved backwards or conflicts")
    if (
        ctx.decision_timestamp_ms is not None
        and state["last_decision_timestamp_ms"] is not None
        and ctx.decision_timestamp_ms < state["last_decision_timestamp_ms"]
    ):
        raise ValueError("level decision clock moved backwards")
    return False


def validate_level_graph_checkpoint(executor, state: dict) -> None:
    """All owners and temporal ancestors belong to one complete post-bar decision."""
    if (
        not isinstance(state, dict)
        or set(state) != {"version", "states", "stepped_bar"}
        or type(state["version"]) is not int
        or state["version"] != 1
        or type(state["stepped_bar"]) is not int
        or state["stepped_bar"] < -1
        or not isinstance(state["states"], dict)
    ):
        raise ValueError("level graph state envelope mismatch")
    states, stepped = state["states"], state["stepped_bar"]
    if stepped == -1:
        if states:
            raise ValueError("initial level graph checkpoint must be empty")
        return
    if set(states) != set(executor._nodes) or any(not isinstance(v, dict) for v in states.values()):
        raise ValueError("level graph checkpoint requires the complete state map")
    reference = states[next(iter(executor._level_bindings))]
    fields = (
        "last_bar_index",
        "last_timestamp_ms",
        "last_decision_timestamp_ms",
        "symbol",
        "timeframe",
        "context_hash",
        "last_primary_valid",
        "last_close",
    )
    for nid in executor._level_bindings:
        owner = states[nid]
        if (
            not owner
            or owner.get("last_bar_index") != stepped
            or tuple(owner.get(k) for k in fields) != tuple(reference.get(k) for k in fields)
        ):
            raise ValueError("level graph checkpoint owner cursor/identity mismatch")
    for nid, node in executor._nodes.items():
        if node.spec.type == "fact.cross":
            _validate_cross_checkpoint(executor, nid, states[nid], reference)
        if node.spec.type in {"state.condition_hold", "fact.condition_edge"}:
            if not states[nid] or any(
                states[nid].get(k) != reference.get(k)
                for k in ("last_bar_index", "last_timestamp_ms", "symbol", "timeframe")
            ):
                raise ValueError("level graph temporal checkpoint cursor/identity mismatch")


def _validate_cross_checkpoint(executor, node_id: str, state: dict, reference: dict) -> None:
    """Crossing history must describe the same completed primary decision."""
    from koval.strategy.nodes.cross import CROSS_STATE_VERSION

    if (
        set(state) != {"version", "last_timestamp_ms", "previous"}
        or type(state["version"]) is not int
        or state["version"] != CROSS_STATE_VERSION
        or type(state["last_timestamp_ms"]) is not int
        or state["last_timestamp_ms"] != reference.get("last_timestamp_ms")
    ):
        raise ValueError("level graph cross checkpoint version/cursor mismatch")
    previous = state["previous"]
    if previous is None:
        return
    if not isinstance(previous, dict) or set(previous) != {"left", "right"}:
        raise ValueError("level graph cross checkpoint requires both previous operands")
    sources = {edge.dst_port: edge.src for edge in executor._incoming[node_id]}
    operands = {port: NumericValueV1.model_validate(value) for port, value in previous.items()}
    for port, value in operands.items():
        if (
            value.readiness != "ready"
            or value.bar_index != reference.get("last_bar_index")
            or value.timestamp_ms != reference.get("last_timestamp_ms")
            or value.symbol != reference.get("symbol")
            or value.timeframe != reference.get("timeframe")
            or value.source_node_id != sources.get(port)
            or reference.get("last_decision_timestamp_ms") is None
            or value.available_at_ms > reference["last_decision_timestamp_ms"]
        ):
            raise ValueError("level graph cross checkpoint operand cursor/identity mismatch")
    if operands["left"].unit != operands["right"].unit:
        raise ValueError("level graph cross checkpoint operand units mismatch")
