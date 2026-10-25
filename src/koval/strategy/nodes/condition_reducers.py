"""Bounded primary condition reducers; checkpoints contain post-bar state only."""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from koval.exchanges.base import timeframe_ms
from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import ConditionV1, MarketEvent, ValueUnit
from koval.strategy.graph.evidence import observe
from koval.strategy.graph.node import NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.nodes.value_contracts import (
    first,
    observation,
    scalar_binding,
    scalar_data,
    scalar_status,
)

HOLD_STATE_VERSION = 1
EDGE_STATE_VERSION = 1
HOLD_FORMULA_VERSION = "condition_hold_v1"
EDGE_FORMULA_VERSION = "condition_edge_v1"


class HoldParamsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    bars: int = Field(ge=1, le=400)


class EdgeParamsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    symbol: str
    timeframe: str | None
    unit: ValueUnit
    formula_version: str = Field(min_length=1)
    source_node_id: str


class _ReducerState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: int = Field(ge=1, le=1)
    last_timestamp_ms: int = Field(ge=0)
    last_bar_index: int = Field(ge=0)
    symbol: str
    timeframe: str | None
    binding: _Binding | None


class _HoldState(_ReducerState):
    formula_version: Literal["condition_hold_v1"]
    bars: int = Field(ge=1, le=400)
    count: int = Field(ge=0, le=400)
    output: ConditionV1

    @model_validator(mode="after")
    def _consistent(self):
        if self.count > self.bars:
            raise ValueError("hold count exceeds configured bars")
        if (
            self.output.timestamp_ms != self.last_timestamp_ms
            or self.output.bar_index != self.last_bar_index
            or self.output.symbol != self.symbol
            or self.output.timeframe != self.timeframe
            or (self.output.readiness != "ready" and self.count != 0)
            or (self.output.readiness == "ready" and self.output.value != (self.count == self.bars))
        ):
            raise ValueError("hold state cursor/output mismatch")
        return self


class _EdgeState(_ReducerState):
    formula_version: Literal["condition_edge_v1"]
    previous: ConditionV1 | None

    @model_validator(mode="after")
    def _consistent(self):
        if self.previous is not None and (
            self.previous.readiness != "ready"
            or self.previous.timestamp_ms != self.last_timestamp_ms
            or self.previous.bar_index != self.last_bar_index
            or self.previous.symbol != self.symbol
            or self.previous.timeframe != self.timeframe
            or self.binding is None
            or scalar_binding(self.previous) != self.binding.model_dump()
        ):
            raise ValueError("edge state cursor/previous mismatch")
        return self


def validate_condition_reducer_state(kind: str, params: BaseModel, state: dict) -> None:
    """Validate a restored reducer before mutation, including same-bar restoration.

    Empty state is the unstepped state. Input binding changes are handled on the
    next new cursor; a reducer's own config/formula cannot reinterpret a snapshot.
    """
    if kind not in {"state.condition_hold", "fact.condition_edge"}:
        return
    if not isinstance(state, dict):
        raise ValueError("condition reducer state must be an object")
    if not state:
        return
    if kind == "state.condition_hold":
        saved = _HoldState.model_validate(state)
        if saved.bars != params.bars:
            raise ValueError("hold state config mismatch")
        if not saved.output.formula_version.startswith(
            HOLD_FORMULA_VERSION + f":{params.model_dump_json()}:"
        ):
            raise ValueError("hold state output formula mismatch")
    else:
        _EdgeState.model_validate(state)


def validate_condition_graph_checkpoint(executor, state: dict) -> None:
    """A reducer graph restores one complete post-bar decision before cache use."""
    reducers = {
        nid: node
        for nid, node in executor._nodes.items()
        if node.spec.type in {"state.condition_hold", "fact.condition_edge"}
    }
    if not reducers:
        return
    if (
        not isinstance(state, dict)
        or set(state) != {"version", "states", "stepped_bar"}
        or type(state["version"]) is not int
        or state["version"] != 1
        or type(state["stepped_bar"]) is not int
        or state["stepped_bar"] < -1
        or not isinstance(state["states"], dict)
    ):
        raise ValueError("condition graph state envelope mismatch")
    states, stepped = state["states"], state["stepped_bar"]
    if stepped == -1:
        if states:
            raise ValueError("initial condition graph checkpoint must be empty")
        return
    if set(states) != set(executor._nodes) or any(not isinstance(v, dict) for v in states.values()):
        raise ValueError("condition graph checkpoint requires the complete state map")
    reference = states[next(iter(reducers))]
    fields = ("last_timestamp_ms", "symbol", "timeframe")
    for nid, node in reducers.items():
        saved = states[nid]
        validate_condition_reducer_state(node.spec.type, node.params, saved)
        if (
            not saved
            or saved["last_bar_index"] != stepped
            or tuple(saved[k] for k in fields) != tuple(reference.get(k) for k in fields)
        ):
            raise ValueError("condition graph checkpoint cursor/identity mismatch")


def _cursor(ctx, value, state):
    """Check consumed cursor before any input status or persistent mutation."""
    binding = None if value is None else scalar_binding(value)
    if state:
        last = state["last_timestamp_ms"]
        if ctx.timestamp_ms < last:
            raise ValueError("condition reducer cursor moved backwards")
        if ctx.timestamp_ms > last and ctx.bar_index <= state["last_bar_index"]:
            raise ValueError("condition reducer primary index moved backwards")
        same_binding = (
            state["symbol"] == ctx.symbol
            and state["timeframe"] == ctx.timeframe
            and state["binding"] == binding
        )
        if ctx.timestamp_ms == last:
            if not same_binding or ctx.bar_index != state["last_bar_index"]:
                raise ValueError("duplicate cursor binding mismatch")
            return binding, True, False
        try:
            interval = timeframe_ms(ctx.timeframe)
        except ValueError:
            interval = None
        consecutive = (
            same_binding
            and interval is not None
            and ctx.timestamp_ms == last + interval
            and ctx.bar_index == state["last_bar_index"] + 1
        )
        return binding, False, consecutive
    return binding, False, False


def _status(ctx, value):
    readiness, reason = scalar_status(ctx, [value])
    if readiness != "ready":
        return readiness, reason
    if not ctx.symbol or not ctx.timeframe:
        return "missing", "missing_identity"
    try:
        step = timeframe_ms(ctx.timeframe)
    except ValueError:
        return "invalid", "unsupported_timeframe"
    if ctx.decision_timestamp_ms < ctx.timestamp_ms + step:
        return "missing", "future_availability"
    if ctx.decision_timestamp_ms >= ctx.timestamp_ms + 2 * step:
        return "stale", "stale_primary_bar"
    return "ready", None


def _input(inputs):
    value = first(inputs, "condition")
    if value is not None and not isinstance(value, ConditionV1):
        raise ValueError("condition reducer requires ConditionV1")
    return value


def _common(ctx, binding):
    return dict(
        version=1,
        last_timestamp_ms=ctx.timestamp_ms,
        last_bar_index=ctx.bar_index,
        symbol=ctx.symbol,
        timeframe=ctx.timeframe,
        binding=binding,
    )


def _hold_factory(params):
    def evaluate(ctx, inputs, state):
        validate_condition_reducer_state("state.condition_hold", params, state)
        value = _input(inputs)
        binding, duplicate, consecutive = _cursor(ctx, value, state)
        if duplicate:
            output = ConditionV1.model_validate(deepcopy(state["output"]))
            observation(ctx, output, "consumed cursor: return prior hold output")
            return {"condition": output}
        readiness, reason = _status(ctx, value)
        count = state["count"] if consecutive else 0
        count = min(params.bars, count + 1) if readiness == "ready" and value.value else 0
        output = ConditionV1(
            **scalar_data(ctx, [value], HOLD_FORMULA_VERSION + ":" + params.model_dump_json()),
            value=count == params.bars if readiness == "ready" else None,
            readiness=readiness,
            reason=reason,
        )
        saved = _HoldState(
            **_common(ctx, binding),
            formula_version=HOLD_FORMULA_VERSION,
            bars=params.bars,
            count=count,
            output=output,
        )
        state.update(saved.model_dump())
        observation(ctx, output, f"{params.bars} consecutive ready true primary conditions")
        observe(ctx, values={"count": count, "required_bars": params.bars})
        return {"condition": output}

    return evaluate


def _edge_factory(params):
    def evaluate(ctx, inputs, state):
        validate_condition_reducer_state("fact.condition_edge", params, state)
        value = _input(inputs)
        binding, duplicate, consecutive = _cursor(ctx, value, state)
        if duplicate:
            observe(
                ctx,
                values={"reason": "duplicate_cursor"},
                predicate="consecutive ready false then true",
                result="recorded",
            )
            return {"event": None}
        readiness, reason = _status(ctx, value)
        previous = state.get("previous") if consecutive else None
        current = value if readiness == "ready" else None
        event = None
        if (
            previous is not None
            and previous["value"] is False
            and current is not None
            and current.value is True
        ):
            event = MarketEvent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="fact.condition_edge",
                kind="condition_edge",
                direction="bullish",
                price_levels=[],
                metadata=deepcopy(
                    {
                        "formula_version": EDGE_FORMULA_VERSION,
                        "state_version": EDGE_STATE_VERSION,
                        "current": current.model_dump(),
                        "previous": previous,
                    }
                ),
            )
        saved = _EdgeState(
            **_common(ctx, binding), formula_version=EDGE_FORMULA_VERSION, previous=current
        )
        state.update(saved.model_dump())
        observe(
            ctx,
            values={
                "readiness": readiness,
                "reason": reason,
                "current": None if value is None else value.model_dump(),
                "previous": deepcopy(previous),
            },
            predicate="consecutive ready false then true",
            result="unavailable" if readiness != "ready" else "passed" if event else "failed",
        )
        return {"event": event}

    return evaluate


register_node(
    NodeSpec(
        "state.condition_hold",
        Domain.STATE,
        "Hold Condition",
        "True after N consecutive ready true primary conditions.",
        HoldParamsV1,
        {"condition": PortSpec("condition", (ConditionV1,))},
        {"condition": PortSpec("condition", (ConditionV1,))},
        _hold_factory,
    )
)
register_node(
    NodeSpec(
        "fact.condition_edge",
        Domain.FACT,
        "Condition Rising Edge",
        "One bullish event after consecutive ready false then true.",
        EdgeParamsV1,
        {"condition": PortSpec("condition", (ConditionV1,))},
        {"event": PortSpec("event", (MarketEvent,), required=False)},
        _edge_factory,
    )
)
