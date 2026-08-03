"""Pure margin helper for the position sizing engine."""

from __future__ import annotations


def required_margin(
    *, quantity: float, entry_price: float, leverage: float, instrument_type: str
) -> float:
    notional = abs(float(quantity)) * float(entry_price)
    if instrument_type == "spot":
        return notional
    lev = float(leverage)
    if lev <= 0:
        lev = 1.0
    return notional / lev
