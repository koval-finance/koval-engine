"""Shared runtime guards and provenance for the new scalar nodes."""

from __future__ import annotations

import json

from koval.strategy.graph.entities import ConditionV1, NumericValueV1


def scalar_binding(value):
    """Stable operand identity, excluding clocks, cursors and measured values."""
    data = value if isinstance(value, dict) else value.model_dump()
    return {
        k: data[k] for k in ("symbol", "timeframe", "unit", "formula_version", "source_node_id")
    }


def scalar_status(ctx, operands, *, price_only=False):
    """Validate identity before evaluation; never coerce an unavailable operand."""
    present = [v for v in operands if v is not None]
    identities = {(v.symbol, v.timeframe, v.unit) for v in present}
    if len(identities) > 1:
        raise ValueError("scalar identity mismatch (symbol/timeframe/unit)")
    for v in present:
        if (v.symbol and v.symbol != ctx.symbol) or (v.timeframe and v.timeframe != ctx.timeframe):
            raise ValueError("scalar identity differs from primary context")
        if price_only and v.unit != "price_quote_per_base":
            raise ValueError("scalar identity requires price units")
    if any(v is None for v in operands):
        return "missing", "missing_input"
    for v in operands:
        if v.readiness != "ready":
            return v.readiness, v.reason
    if ctx.decision_timestamp_ms is None:
        return "missing", "missing_timing"
    for v in operands:
        if v.timestamp_ms != ctx.timestamp_ms or v.bar_index != ctx.bar_index:
            return "stale", "stale_input_cursor"
        if v.available_at_ms > ctx.decision_timestamp_ms:
            return "missing", "future_availability"
    return "ready", None


def scalar_data(ctx, operands, formula, *, unit=None):
    present = [v for v in operands if v is not None]
    stamps = [v.as_of_ms for v in present if v.as_of_ms is not None]
    availability = [v.available_at_ms for v in present if v.available_at_ms is not None]
    return dict(
        bar_index=ctx.bar_index,
        timestamp_ms=ctx.timestamp_ms,
        source_node_id=formula,
        unit=unit or (present[0].unit if present else "dimensionless"),
        symbol=ctx.symbol,
        timeframe=ctx.timeframe,
        as_of_ms=max(stamps) if stamps else None,
        available_at_ms=max(availability) if availability else None,
        formula_version=formula
        + ":"
        + json.dumps(
            [scalar_binding(v) if v is not None else None for v in operands],
            sort_keys=True,
            separators=(",", ":"),
        ),
        input_cursor=json.dumps(
            [v.input_cursor if v is not None else None for v in operands], separators=(",", ":")
        ),
    )


def first(inputs, name):
    values = inputs.get(name, [])
    if len(values) > 1:
        raise ValueError(f"scalar port {name} accepts at most one value")
    return values[0] if values else None


def observation(ctx, entity, predicate):
    from koval.strategy.graph.evidence import observe

    observe(
        ctx,
        values=entity.model_dump(),
        predicate=predicate,
        result="unavailable"
        if entity.readiness != "ready"
        else "passed"
        if isinstance(entity, ConditionV1) and entity.value
        else "failed"
        if isinstance(entity, ConditionV1)
        else "recorded",
    )


def numeric_result(ctx, operands, value, readiness, reason, formula, unit):
    result = NumericValueV1(
        **scalar_data(ctx, operands, formula, unit=unit),
        value=value,
        readiness=readiness,
        reason=reason,
    )
    observation(ctx, result, formula)
    return result
