from __future__ import annotations


def trailing_stop_price(
    *,
    direction: str,
    current_price: float,
    trail_pct: float,
    current_stop: float,
) -> float:
    """
    Calculate new trailing stop price.
    The stop only moves in the profitable direction — never against the position.
    Pass current_stop=0.0 on first call to get the initial stop level.
    """
    trail_dist = current_price * trail_pct / 100.0
    if direction == "long":
        new_stop = current_price - trail_dist
        return float(max(new_stop, current_stop))
    else:
        new_stop = current_price + trail_dist
        return float(min(new_stop, current_stop)) if current_stop > 0.0 else float(new_stop)
