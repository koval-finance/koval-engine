"""Pure pricing helpers for the order pricing constructor.

No state, no Backtrader. Each returns absolute prices (or ``None`` when the chosen
model cannot be satisfied from the given geometry, so the node can fall back).
"""

from __future__ import annotations


def structural_sl(*, price_levels: list[float], direction: str, entry: float) -> float | None:
    """Stop at the nearest protective structural level (below entry for long,
    above for short). ``None`` if no level is on the protective side."""
    if direction == "long":
        below = [lvl for lvl in price_levels if lvl < entry]
        return max(below) if below else None
    above = [lvl for lvl in price_levels if lvl > entry]
    return min(above) if above else None


def volatility_sl(*, entry: float, atr: float, mult: float, direction: str) -> float:
    """Stop at ``mult`` ATRs from entry, on the protective side."""
    distance = abs(float(atr)) * float(mult)
    return entry - distance if direction == "long" else entry + distance


def liquidity_target(*, price_levels: list[float], direction: str, entry: float) -> float | None:
    """Target at the nearest liquidity level in the profit direction. ``None`` if none."""
    if direction == "long":
        above = [lvl for lvl in price_levels if lvl > entry]
        return min(above) if above else None
    below = [lvl for lvl in price_levels if lvl < entry]
    return max(below) if below else None


def zone_entry(*, price_levels: list[float], direction: str, offset_pct: float) -> float | None:
    """Limit entry anchored to the nearest zone level, offset by ``offset_pct``
    toward the favourable side. ``None`` if no level supplied."""
    if not price_levels:
        return None
    anchor = price_levels[0]
    factor = 1.0 - offset_pct / 100.0 if direction == "long" else 1.0 + offset_pct / 100.0
    return float(anchor) * factor
