"""Primary historical ideal-close values. No observed-receive or alternate-feed binding.

The host supplies a contiguous, closed, chronological history and its actual clocks.
EMA/ATR use the entire retained prefix, including after an offset; no new seed/window.
"""

from __future__ import annotations

import json
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from koval.exchanges.base import timeframe_ms
from koval.strategy.graph.entities import NumericValueV1, ValueUnit
from koval.strategy.graph.node import BarContext
from koval.strategy.helpers._math import _atr, _bollinger, _ema

ValueComponent = Literal[
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ema",
    "atr",
    "bb.middle",
    "bb.upper",
    "bb.lower",
    "bb.width_pct",
    "bb.zscore",
    "channel.lower",
    "channel.upper",
    "channel.center",
]
VALUE_FORMULA_VERSION = "numeric_value_v1"
VALUE_CAPABILITY = "numeric_values_v1"


class ValueSpecV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    component: ValueComponent
    period: int | None = Field(default=None, ge=2, le=400)
    std_dev: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    offset_bars: int = Field(default=0, ge=0)
    context: Literal["primary"] = "primary"

    @model_validator(mode="after")
    def _variant(self):
        windowed = self.component in {"ema", "atr"} or "." in self.component
        if not windowed and self.period is not None:
            raise ValueError("period is unsupported for OHLCV")
        if not self.component.startswith("bb.") and self.std_dev is not None:
            raise ValueError("std_dev is supported only for BB")
        if windowed and self.period is None:
            object.__setattr__(self, "period", 14 if self.component == "atr" else 20)
        if self.component.startswith("bb.") and self.std_dev is None:
            object.__setattr__(self, "std_dev", 2.0)
        return self

    def required_history_bars(self) -> int:
        count = self.period or 1
        if self.component == "atr" or self.component.startswith("channel."):
            count += 1
        return count + self.offset_bars

    @property
    def unit(self) -> ValueUnit:
        if self.component == "volume":
            return "base_volume"
        if self.component == "bb.width_pct":
            return "percent"
        if self.component == "bb.zscore":
            return "dimensionless"
        return "price_quote_per_base"


def _timing(ctx, spec, size):
    if not ctx.symbol or not ctx.timeframe:
        return None, None, "missing", "missing_identity"
    if ctx.decision_timestamp_ms is None or ctx.history_start_ms is None:
        return None, None, "missing", "missing_timing"
    try:
        step = timeframe_ms(ctx.timeframe)
    except ValueError:
        return None, None, "invalid", "unsupported_timeframe"
    as_of = ctx.timestamp_ms + step * (1 - spec.offset_bars)
    if as_of < 0:
        return None, None, "warming_up", "insufficient_history"
    # A grid check detects a truncated/holey declared span. The host still owns
    # validation of the individual timestamps (not carried in BarContext arrays).
    if size and ctx.history_start_ms + (size - 1) * step != ctx.timestamp_ms:
        return as_of, as_of, "invalid", "noncontiguous_history"
    if ctx.timestamp_ms + step > ctx.decision_timestamp_ms:
        return as_of, as_of, "missing", "future_availability"
    if ctx.decision_timestamp_ms >= ctx.timestamp_ms + 2 * step:
        return as_of, as_of, "stale", "stale_primary_bar"
    return as_of, as_of, "ready", None


def _arrays(ctx, spec, end):
    component = spec.component
    if component in {"ema", "close"} or component.startswith("bb."):
        names = ["closes"]
    elif component == "atr":
        names = ["highs", "lows", "closes"]
    elif component.startswith("channel."):
        names = ["highs", "lows"]
    else:
        names = [{"open": "opens", "high": "highs", "low": "lows", "volume": "volumes"}[component]]
    result = {}
    for name in names:
        raw = getattr(ctx, name)
        if raw is None:
            return {}, "missing", f"missing_{name}"
        a = np.asarray(raw, dtype=float)
        if a.ndim != 1 or len(a) != len(ctx.closes):
            return {}, "invalid", "misaligned_history"
        a = a[:end]
        # Only recursive EMA/ATR depend on the whole retained prefix.
        if component not in {"ema", "atr"}:
            a = a[-(spec.required_history_bars() - spec.offset_bars) :]
        if component.startswith("channel."):
            a = a[:-1]
        if not np.all(np.isfinite(a)):
            return {}, "invalid", "nonfinite_input"
        result[name] = a
    return result, "ready", None


def _calculate(spec, arrays):
    component, period = spec.component, spec.period
    if component == "ema":
        return float(_ema(arrays["closes"], period)[-1]), None
    if component == "atr":
        return _atr(arrays["highs"], arrays["lows"], arrays["closes"], period), None
    if component.startswith("channel."):
        upper, lower = float(max(arrays["highs"])), float(min(arrays["lows"]))
        return {
            "channel.upper": upper,
            "channel.lower": lower,
            "channel.center": (upper + lower) / 2,
        }[component], None
    if component.startswith("bb."):
        closes = arrays["closes"]
        middle, sigma, upper, lower = _bollinger(closes, period, spec.std_dev)
        if component == "bb.width_pct":
            return ((upper - lower) / middle * 100.0, None) if middle else (None, "zero_middle")
        if component == "bb.zscore":
            if not np.isfinite(middle) or not np.isfinite(sigma):
                return None, "nonfinite_intermediate"
            return (
                ((float(closes[-1]) - middle) / sigma, None) if sigma else (None, "zero_variance")
            )
        return {"bb.middle": middle, "bb.upper": upper, "bb.lower": lower}[component], None
    return float(next(iter(arrays.values()))[-1]), None


def value_at(ctx: BarContext, spec: ValueSpecV1) -> NumericValueV1:
    """Evaluate one value using historical ideal-close availability, or explicit null.

    Legacy prepared pairs have no timing/provenance binding and are deliberately
    ignored. Direct, prepared-strategy, chart and recorded output use this helper.
    """
    size = 0 if ctx.closes is None else len(ctx.closes)
    as_of, available, readiness, reason = _timing(ctx, spec, size)
    cursor = json.dumps(
        {
            "formula": VALUE_FORMULA_VERSION,
            "spec": spec.model_dump(),
            "symbol": ctx.symbol,
            "timeframe": ctx.timeframe,
            "bar_open_ms": ctx.timestamp_ms,
            "history_start_ms": ctx.history_start_ms,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    result = None
    if readiness == "ready":
        if ctx.closes is None:
            readiness, reason = "missing", "missing_closes"
        elif size < spec.required_history_bars():
            readiness, reason = "warming_up", "insufficient_history"
        else:
            arrays, readiness, reason = _arrays(ctx, spec, size - spec.offset_bars)
            if readiness == "ready":
                with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                    result, reason = _calculate(spec, arrays)
                if reason:
                    readiness = "missing" if reason == "zero_variance" else "invalid"
                elif not np.isfinite(result):
                    result, readiness, reason = None, "invalid", "nonfinite_result"
    if spec.component.startswith("channel.") and as_of is not None:
        as_of -= timeframe_ms(ctx.timeframe)
        available = as_of
        if as_of < 0:
            as_of = available = None
    return NumericValueV1(
        bar_index=ctx.bar_index,
        timestamp_ms=ctx.timestamp_ms,
        source_node_id="source.value",
        value=result,
        unit=spec.unit,
        symbol=ctx.symbol,
        timeframe=ctx.timeframe,
        as_of_ms=as_of,
        available_at_ms=available,
        readiness=readiness,
        reason=reason,
        formula_version=VALUE_FORMULA_VERSION + ":" + spec.model_dump_json(),
        input_cursor=cursor,
    )


def graph_required_history_bars(graph: dict) -> int:
    """Minimum retained primary buffer; crossings also need two ready steps."""
    return max(
        (
            ValueSpecV1(**(b.get("params") or {})).required_history_bars()
            for b in graph.get("blocks", [])
            if b.get("type") == "source.value"
        ),
        default=0,
    )
