"""Pure strict price predicates for frozen bullish historical setups."""

from math import isfinite


def level_center(lower: float, upper: float) -> float:
    """Preserve subnormal sums; avoid overflow for large positive finite bounds."""
    center = (lower + upper) / 2
    return center if isfinite(center) else lower / 2 + upper / 2


def level_invalidated(opening: float, close: float, lower: float, buffer_pct: float) -> bool:
    threshold = lower * (1.0 - buffer_pct / 100.0)
    return opening < threshold or close < threshold


def sweep_reclaimed(low: float, close: float, lower: float) -> bool:
    return low < lower and close > lower


def breakout_retested(low: float, close: float, level: float, tolerance_pct: float) -> bool:
    return low <= level * (1.0 + tolerance_pct / 100.0) and close > level
