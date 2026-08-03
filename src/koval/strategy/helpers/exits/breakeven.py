from __future__ import annotations


def breakeven_trigger(
    *,
    entry_price: float,
    direction: str,
    current_price: float,
    stop_loss: float,
    trigger_r: float = 1.0,
) -> float | None:
    """
    Return entry_price (move SL to breakeven) if price has moved trigger_r × risk
    in the profitable direction. Otherwise return None.
    """
    sl_distance = abs(entry_price - stop_loss)
    if sl_distance == 0.0:
        return None
    if direction == "long":
        profit = current_price - entry_price
    else:
        profit = entry_price - current_price
    if profit >= sl_distance * trigger_r:
        return float(entry_price)
    return None
