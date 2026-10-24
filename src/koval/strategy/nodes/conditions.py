"""Typed scalar comparison, signed distance and explicit policy conversion."""

from __future__ import annotations

from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import ConditionV1, NumericValueV1, PolicyDecision, ValueUnit
from koval.strategy.graph.evidence import observe
from koval.strategy.graph.node import NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.nodes.value_contracts import (
    first,
    numeric_result,
    observation,
    scalar_data,
    scalar_status,
)


class CompareParamsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    operator: Literal["gt", "gte", "lt", "lte", "eq", "ne", "between"]
    literal: float | None = Field(default=None, allow_inf_nan=False)
    unit: ValueUnit | None = None
    lower: float | None = Field(default=None, allow_inf_nan=False)
    upper: float | None = Field(default=None, allow_inf_nan=False)
    inclusive: bool = True

    @model_validator(mode="after")
    def _variant(self):
        if self.operator == "between":
            if self.lower is None or self.upper is None or self.lower > self.upper:
                raise ValueError("between requires ordered finite lower/upper bounds")
            if self.literal is not None or self.unit is None:
                raise ValueError("between requires units and forbids literal/right")
        else:
            if self.lower is not None or self.upper is not None or not self.inclusive:
                raise ValueError("bounds/inclusive are supported only for between")
            if (self.literal is None) != (self.unit is None):
                raise ValueError("literal and unit must be specified together")
        return self


def _compare_factory(p):
    def evaluate(ctx, inputs, state):
        left, right = first(inputs, "left"), first(inputs, "right")
        if right is not None and (p.operator == "between" or p.literal is not None):
            raise ValueError("right conflicts with literal/between")
        if left is not None and p.unit is not None and left.unit != p.unit:
            raise ValueError("literal unit identity mismatch")
        operands = (
            [left, right]
            if right is not None or (p.operator != "between" and p.literal is None)
            else [left]
        )
        readiness, reason = scalar_status(ctx, operands)
        result = None
        if readiness == "ready":
            a, b = left.value, right.value if right is not None else p.literal
            if p.operator == "between":
                result = p.lower <= a <= p.upper if p.inclusive else p.lower < a < p.upper
            else:
                result = {
                    "gt": a > b,
                    "gte": a >= b,
                    "lt": a < b,
                    "lte": a <= b,
                    "eq": a == b,
                    "ne": a != b,
                }[p.operator]
        entity = ConditionV1(
            **scalar_data(ctx, operands, "compare_v1:" + p.model_dump_json()),
            value=result,
            readiness=readiness,
            reason=reason,
        )
        observation(
            ctx, entity, f"{p.operator} ({'inclusive' if p.inclusive else 'exclusive'} bounds)"
        )
        return {"condition": entity}

    return evaluate


class DistanceParamsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    mode: Literal["percent", "atr"]


def _distance_factory(p):
    def evaluate(ctx, inputs, state):
        operands = [first(inputs, "price"), first(inputs, "level")]
        atr = first(inputs, "atr")
        if p.mode == "percent" and atr is not None:
            raise ValueError("percent distance forbids atr input")
        if p.mode == "atr":
            operands.append(atr)
        readiness, reason = scalar_status(ctx, operands, price_only=True)
        result = None
        if readiness == "ready":
            denominator = operands[-1].value
            if denominator <= 0:
                readiness, reason = "invalid", "nonpositive_denominator"
            else:
                result = (operands[0].value - operands[1].value) / denominator
                if p.mode == "percent":
                    result *= 100.0
                if not isfinite(result):
                    result, readiness, reason = None, "invalid", "nonfinite_result"
        return {
            "value": numeric_result(
                ctx,
                operands,
                result,
                readiness,
                reason,
                f"distance_{p.mode}_v1",
                "percent" if p.mode == "percent" else "ratio",
            )
        }

    return evaluate


class FromConditionParamsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _bridge_factory(p):
    def evaluate(ctx, inputs, state):
        condition = first(inputs, "condition")
        readiness, reason = scalar_status(ctx, [condition])
        allowed = readiness == "ready" and condition.value is True
        data = scalar_data(ctx, [condition], "from_condition_v1")
        metadata = {
            **data,
            "readiness": readiness,
            "reason": reason,
            "condition": None if condition is None else condition.model_dump(),
        }
        result = PolicyDecision(
            bar_index=ctx.bar_index,
            timestamp_ms=ctx.timestamp_ms,
            source_node_id="policy.from_condition",
            allowed=allowed,
            reason=reason if readiness != "ready" else None if allowed else "condition_false",
            metadata=metadata,
        )
        observe(
            ctx,
            values=metadata,
            predicate="ready and condition.value is true",
            result="passed" if allowed else "failed" if readiness == "ready" else "unavailable",
        )
        return {"policy": result}

    return evaluate


_NUMERIC = (NumericValueV1,)
register_node(
    NodeSpec(
        "fact.compare",
        Domain.FACT,
        "Compare Values",
        "Typed exact comparison or literal interval.",
        CompareParamsV1,
        {"left": PortSpec("left", _NUMERIC), "right": PortSpec("right", _NUMERIC, required=False)},
        {"condition": PortSpec("condition", (ConditionV1,))},
        _compare_factory,
    )
)
register_node(
    NodeSpec(
        "fact.distance",
        Domain.FACT,
        "Distance",
        "Signed percent or ATR distance between prices.",
        DistanceParamsV1,
        {
            "price": PortSpec("price", _NUMERIC),
            "level": PortSpec("level", _NUMERIC),
            "atr": PortSpec("atr", _NUMERIC, required=False),
        },
        {"value": PortSpec("value", _NUMERIC)},
        _distance_factory,
    )
)
register_node(
    NodeSpec(
        "policy.from_condition",
        Domain.POLICY,
        "Condition Policy",
        "Allow only a ready true condition.",
        FromConditionParamsV1,
        {"condition": PortSpec("condition", (ConditionV1,))},
        {"policy": PortSpec("policy", (PolicyDecision,))},
        _bridge_factory,
    )
)
