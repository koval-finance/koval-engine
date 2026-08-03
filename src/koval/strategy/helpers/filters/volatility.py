from __future__ import annotations

import numpy as np

from koval.strategy.helpers._math import _atr


def atr_volatility_filter(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
    min_atr_pct: float = 0.5,
) -> bool:
    """True if ATR as a percentage of current close >= min_atr_pct."""
    atr = _atr(highs, lows, closes, period)
    if atr == 0.0:
        return False
    current_close = float(closes[-1])
    if current_close == 0.0:
        return False
    return (atr / current_close) * 100.0 >= min_atr_pct


def bb_volatility_filter(
    closes: np.ndarray,
    period: int = 20,
    std_dev: float = 2.0,
    min_bandwidth_pct: float = 1.0,
) -> bool:
    """True if Bollinger Band bandwidth >= min_bandwidth_pct%."""
    if len(closes) < period:
        return False
    recent = closes[-period:]
    middle = float(recent.mean())
    if middle == 0.0:
        return False
    std = float(recent.std())
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    bandwidth_pct = (upper - lower) / middle * 100.0
    return bandwidth_pct >= min_bandwidth_pct
