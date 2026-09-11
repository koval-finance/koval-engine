"""Shared validation of a dynamic protective-bracket snapshot."""

from decimal import Decimal, InvalidOperation


def validate_protection_update(
    *,
    side: str,
    current_stop: float | str,
    stop_price: float | str,
    target_price: float | str,
) -> None:
    """Allow either target direction, but never widen stop risk or cross legs.

    Validate both requested levels before mutating either. The entry price is
    deliberately irrelevant: a trailing stop can lock in a profit.
    """
    try:
        old, stop, target = (Decimal(str(v)) for v in (current_stop, stop_price, target_price))
    except InvalidOperation as exc:
        raise ValueError("protection prices must be positive and finite") from exc
    if any(not v.is_finite() or v <= 0 for v in (old, stop, target)):
        raise ValueError("protection prices must be positive and finite")
    if side not in {"buy", "sell"}:
        raise ValueError("protection side must be buy or sell")
    if (side == "buy" and stop < old) or (side == "sell" and stop > old):
        raise ValueError("stop update must tighten protection, not increase risk")
    if (side == "buy" and stop >= target) or (side == "sell" and stop <= target):
        raise ValueError("stop update must remain inside the protective target")


__all__ = ["validate_protection_update"]
