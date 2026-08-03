from __future__ import annotations


def fixed_sl_tp(
    *,
    entry_price: float,
    direction: str,
    sl_pct: float = 2.0,
    risk_reward: float = 2.0,
    tp_pct: float | None = None,
) -> tuple[float, float]:
    """
    Calculate fixed SL and TP as absolute prices.

    Returns (stop_loss, take_profit).
    sl_pct: stop loss distance as % of entry price.
    risk_reward: TP = SL distance * risk_reward (ignored if tp_pct is set).
    tp_pct: explicit TP distance as % of entry price (overrides risk_reward).
    """
    effective_tp_pct = tp_pct if tp_pct is not None else sl_pct * risk_reward
    if direction == "long":
        stop_loss = entry_price * (1.0 - sl_pct / 100.0)
        take_profit = entry_price * (1.0 + effective_tp_pct / 100.0)
    else:
        stop_loss = entry_price * (1.0 + sl_pct / 100.0)
        take_profit = entry_price * (1.0 - effective_tp_pct / 100.0)
    return float(stop_loss), float(take_profit)
