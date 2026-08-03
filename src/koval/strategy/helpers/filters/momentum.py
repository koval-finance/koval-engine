from __future__ import annotations

import numpy as np

from koval.strategy.helpers._math import _macd, _rsi


def rsi_filter(
    closes: np.ndarray,
    period: int = 14,
    min_val: float = 30.0,
    max_val: float = 70.0,
) -> bool:
    """True if current RSI is within [min_val, max_val]."""
    if len(closes) < period + 1:
        return False
    val = _rsi(closes, period)
    return min_val <= val <= max_val


def stoch_filter(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    k_period: int = 14,
    min_val: float = 20.0,
    max_val: float = 80.0,
) -> bool:
    """True if Stochastic %K is within [min_val, max_val]."""
    if len(closes) < k_period:
        return False
    recent_high = float(highs[-k_period:].max())
    recent_low = float(lows[-k_period:].min())
    if recent_high == recent_low:
        return False
    k = 100.0 * (float(closes[-1]) - recent_low) / (recent_high - recent_low)
    return min_val <= k <= max_val


def macd_filter(
    closes: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
    require_positive: bool = True,
) -> bool:
    """
    True if MACD histogram sign matches require_positive.
    require_positive=True  -> histogram > 0 (bullish momentum).
    require_positive=False -> histogram < 0 (bearish momentum).
    """
    _, _, hist = _macd(closes, fast, slow, signal_period)
    if abs(hist) < 1e-9:  # treat near-zero as no signal
        return False
    return hist > 0 if require_positive else hist < 0
