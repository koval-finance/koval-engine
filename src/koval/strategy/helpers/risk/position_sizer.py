from __future__ import annotations

import math


def calculate_position_size(
    *,
    account_value: float,
    risk_per_trade_pct: float,
    entry_price: float,
    stop_loss: float,
    direction: str,
    leverage: float = 1.0,
) -> float:
    """
    Calculate position size based on risk management parameters.

    Formula: size = (account_value * risk_pct/100) / abs(entry - stop_loss)

    Leverage is validated for compatibility with existing callers, but it does
    not increase risk-based quantity. Leverage changes required margin; it does
    not change the price-distance loss when the stop is reached.

    Quantity is capped so ``quantity * entry / leverage <= account_value``.
    Returns 0.0 for invalid or non-finite input.

    Args:
        account_value: Trading account equity in base currency.
        risk_per_trade_pct: Risk percentage per trade (e.g., 1.0 for 1%).
        entry_price: Entry price for the position.
        stop_loss: Stop-loss price.
        direction: Trade direction, "long"/"buy" or "short"/"sell".
        leverage: Positive position leverage used by downstream margin checks.

    Returns:
        Position size (float). Zero if any input is invalid.
    """
    try:
        entry = float(entry_price)
        stop = float(stop_loss)
        equity = float(account_value)
        risk_pct = float(risk_per_trade_pct)
        lev = float(leverage)
    except (TypeError, ValueError):
        return 0.0

    if not all(math.isfinite(value) for value in (entry, stop, equity, risk_pct, lev)):
        return 0.0
    if entry <= 0 or stop <= 0 or equity <= 0 or risk_pct <= 0 or lev <= 0:
        return 0.0

    side = direction.lower() if direction else ""
    if side not in {"long", "buy", "short", "sell"}:
        return 0.0
    if side in ("long", "buy") and stop >= entry:
        return 0.0
    if side in ("short", "sell") and stop <= entry:
        return 0.0

    sl_distance = abs(entry - stop)
    if sl_distance == 0.0:
        return 0.0

    risk_amount = equity * (risk_pct / 100.0)
    risk_limited_size = risk_amount / sl_distance
    margin_limited_size = equity * lev / entry
    return float(min(risk_limited_size, margin_limited_size))
