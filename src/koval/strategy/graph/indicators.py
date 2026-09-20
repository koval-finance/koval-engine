"""Optional prepared indicator values for closed-bar graph contexts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from koval.engine.history_window import resolve_history_bars
from koval.strategy.helpers._math import _atr, _ema, _rsi
from koval.strategy.helpers.rolling_indicators import rolling_atr, rolling_ema, rolling_rsi

if TYPE_CHECKING:
    from koval.strategy.graph.node import BarContext

IndicatorValues = dict[tuple[str, int], tuple[float, float]]
_NODE_INDICATORS = {
    "fact.ema_cross": ("ema", ("fast", "slow")),
    "fact.rsi_cross": ("rsi", ("period",)),
    "state.trend_bias": ("ema", ("period",)),
    "state.volatility_regime": ("atr", ("period",)),
    "policy.ema_trend": ("ema", ("period",)),
    "policy.atr_volatility": ("atr", ("period",)),
}


class PreparedIndicators:
    """Per-run tables; a context receives only its own timestamp's scalar pairs."""

    def __init__(self, timestamps, history_bars, values):
        self._timestamps = timestamps.copy()
        self._window = history_bars
        self._values = values

    @classmethod
    def build(cls, graph: dict, candles: np.ndarray, history_bars: int) -> PreparedIndicators:
        # Registry imports nodes, which import these accessors.
        from koval.strategy.graph.registry import get_node

        window = resolve_history_bars(history_bars)
        if not np.all(np.isfinite(candles)) or np.any(np.diff(candles[:, 0]) <= 0):
            return cls(candles[:, 0], window, {})
        required = set()
        for block in graph["blocks"]:
            requirement = _NODE_INDICATORS.get(block["type"])
            if requirement is None:
                continue
            kind, names = requirement
            params = get_node(block["type"]).params_schema(**(block.get("params") or {}))
            required.update((kind, getattr(params, name)) for name in names)
        closes = np.ascontiguousarray(candles[:, 4], dtype=float)
        values = {}
        for kind, period in sorted(required):
            if kind == "ema":
                pairs = rolling_ema(closes, period, window)
            elif kind == "rsi":
                pairs = rolling_rsi(closes, period, window)
            else:
                # Match the host's float64 history injection before subtraction,
                # including float32 or integer caller-supplied feeds.
                atr = rolling_atr(
                    np.asarray(candles[:, 2], dtype=float),
                    np.asarray(candles[:, 3], dtype=float),
                    closes,
                    period,
                    window,
                )
                pairs = np.column_stack((atr, atr))
            pairs.flags.writeable = False
            values[kind, period] = pairs
        return cls(candles[:, 0], window, values)

    def at(self, timestamp_ms: int, history_size: int) -> IndicatorValues | None:
        if not self._values:
            return None
        index = int(np.searchsorted(self._timestamps, timestamp_ms))
        if (
            index == len(self._timestamps)
            or self._timestamps[index] != timestamp_ms
            or history_size != min(index + 1, self._window)
        ):
            return None
        return {
            key: (float(values[index, 0]), float(values[index, 1]))
            for key, values in self._values.items()
        }


def ema_pair(ctx: BarContext, period: int) -> tuple[float, float]:
    if ctx.indicators is not None and ("ema", period) in ctx.indicators:
        return ctx.indicators["ema", period]
    values = _ema(ctx.closes, period)
    return (float(values[-2]) if len(values) > 1 else float("nan"), float(values[-1]))


def rsi_pair(ctx: BarContext, period: int) -> tuple[float, float]:
    if ctx.indicators is not None and ("rsi", period) in ctx.indicators:
        return ctx.indicators["rsi", period]
    return _rsi(ctx.closes[:-1], period), _rsi(ctx.closes, period)


def ema_trend(ctx: BarContext, period: int, direction: str) -> bool:
    current = ema_pair(ctx, period)[1]
    close = float(ctx.closes[-1])
    return close > current if direction == "bullish" else close < current


def atr_allowed(ctx: BarContext, period: int, min_atr_pct: float) -> bool:
    if ctx.indicators is not None and ("atr", period) in ctx.indicators:
        atr = ctx.indicators["atr", period][1]
    else:
        atr = _atr(ctx.highs, ctx.lows, ctx.closes, period)
    close = float(ctx.closes[-1])
    return atr != 0.0 and close != 0.0 and (atr / close) * 100.0 >= min_atr_pct
