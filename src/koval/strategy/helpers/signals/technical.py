from __future__ import annotations

import numpy as np

from koval.strategy.helpers._math import _ema, _rsi


def ema_cross(closes: np.ndarray, fast: int = 9, slow: int = 21) -> str:
    """
    Detect EMA golden/death cross on the last two bars.
    Returns 'bullish' on golden cross, 'bearish' on death cross, 'none' otherwise.
    Requires at least slow + 1 bars.
    """
    if len(closes) < slow + 1:
        return "none"
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    if np.isnan(fast_ema[-2]) or np.isnan(slow_ema[-2]):
        return "none"
    prev_above = fast_ema[-2] > slow_ema[-2]
    curr_above = fast_ema[-1] > slow_ema[-1]
    if not prev_above and curr_above:
        return "bullish"
    if prev_above and not curr_above:
        return "bearish"
    return "none"


def rsi_value(closes: np.ndarray, period: int = 14) -> float:
    """Current RSI value. Returns 50.0 if insufficient data."""
    return _rsi(closes, period)


def rsi_cross(
    closes: np.ndarray,
    period: int = 14,
    level: float = 50.0,
    direction: str = "cross_up",
) -> bool:
    """
    True if RSI crossed `level` in the given direction on the last bar.
    direction: 'cross_up' or 'cross_down'.
    """
    if len(closes) < period + 2:
        return False
    prev_rsi = _rsi(closes[:-1], period)
    curr_rsi = _rsi(closes, period)
    if direction == "cross_up":
        return prev_rsi < level <= curr_rsi
    if direction == "cross_down":
        return prev_rsi > level >= curr_rsi
    return False
