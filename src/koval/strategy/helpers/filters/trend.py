from __future__ import annotations

import numpy as np

from koval.strategy.helpers._math import _ema, _true_range


def adx_filter(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
    min_adx: float = 25.0,
) -> bool:
    """True if ADX >= min_adx (Wilder-smoothed Average Directional Index)."""
    if len(closes) < period * 2 + 1:
        return False
    tr = _true_range(highs, lows, closes)
    n = len(closes)

    # Compute raw +DM and -DM per bar
    dm_plus = np.zeros(n)
    dm_minus = np.zeros(n)
    for i in range(1, n):
        up = float(highs[i]) - float(highs[i - 1])
        down = float(lows[i - 1]) - float(lows[i])
        if up > down and up > 0:
            dm_plus[i] = up
        if down > up and down > 0:
            dm_minus[i] = down

    # Wilder-smooth TR, +DM, -DM (seed with sum of first period)
    smooth_tr = float(tr[1 : period + 1].sum())
    smooth_plus = float(dm_plus[1 : period + 1].sum())
    smooth_minus = float(dm_minus[1 : period + 1].sum())

    dx_values = []
    for i in range(period + 1, n):
        smooth_tr = smooth_tr - smooth_tr / period + float(tr[i])
        smooth_plus = smooth_plus - smooth_plus / period + float(dm_plus[i])
        smooth_minus = smooth_minus - smooth_minus / period + float(dm_minus[i])
        if smooth_tr == 0.0:
            continue
        di_plus = 100.0 * smooth_plus / smooth_tr
        di_minus = 100.0 * smooth_minus / smooth_tr
        denom = di_plus + di_minus
        if denom == 0.0:
            dx_values.append(0.0)
        else:
            dx_values.append(100.0 * abs(di_plus - di_minus) / denom)

    if len(dx_values) < period:
        return False

    # Wilder-smooth DX values into ADX (seed with mean of first period DX values)
    adx = float(np.mean(dx_values[:period]))
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period

    return adx >= min_adx


def ema_trend_filter(
    closes: np.ndarray,
    period: int = 200,
    direction: str = "bullish",
) -> bool:
    """
    True if current price is on the correct side of the EMA.
    direction='bullish': close > EMA.
    direction='bearish': close < EMA.
    Returns False if insufficient data.
    """
    if len(closes) < period:
        return False
    ema_vals = _ema(closes, period)
    current_ema = float(ema_vals[-1])
    if np.isnan(current_ema):
        return False
    current_close = float(closes[-1])
    if direction == "bullish":
        return current_close > current_ema
    return current_close < current_ema
