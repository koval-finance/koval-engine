"""Venue symbol metadata validation for sandbox order routing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from koval.engine.broker import BrokerOrderIntent


@dataclass(frozen=True)
class VenueSymbolMetadata:
    symbol: str
    status: str
    price_tick: str
    quantity_step: str
    min_qty: str
    min_notional: str
    allowed_order_types: tuple[str, ...]
    price_precision: int | None = None
    quantity_precision: int | None = None
    max_notional: str | None = None
    market_quantity_step: str | None = None
    market_min_qty: str | None = None


@dataclass(frozen=True)
class VenueValidationResult:
    ok: bool
    reason: str | None = None
    normalized_price: str | None = None
    normalized_quantity: str | None = None
    normalized_stop_price: str | None = None
    normalized_target_price: str | None = None


def _dec(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _is_multiple(value: Decimal, step: Decimal) -> bool:
    if not value.is_finite() or not step.is_finite() or step <= 0:
        return False
    return value % step == 0


def _format(value: Decimal, precision: int | None) -> str:
    if precision is None:
        return format(value.normalize(), "f")
    return f"{value:.{precision}f}"


def validate_order_against_metadata(
    intent: BrokerOrderIntent,
    metadata: VenueSymbolMetadata | None,
) -> VenueValidationResult:
    if metadata is None:
        return VenueValidationResult(False, "missing_metadata")
    if metadata.status.upper() not in {"TRADING", "ENABLED", "TRUE"}:
        return VenueValidationResult(False, "symbol_not_trading")
    if intent.side not in {"buy", "sell"}:
        return VenueValidationResult(False, "invalid_side")
    if intent.order_type not in set(metadata.allowed_order_types):
        return VenueValidationResult(False, "unsupported_order_type")

    qty = _dec(intent.quantity)
    quantity_step = (
        metadata.market_quantity_step
        if intent.order_type == "market" and metadata.market_quantity_step is not None
        else metadata.quantity_step
    )
    minimum_quantity = (
        metadata.market_min_qty
        if intent.order_type == "market" and metadata.market_min_qty is not None
        else metadata.min_qty
    )
    step = _dec(quantity_step)
    min_qty = _dec(minimum_quantity)
    min_notional = _dec(metadata.min_notional)
    max_notional = _dec(metadata.max_notional)
    if qty is None or not qty.is_finite() or qty <= 0:
        return VenueValidationResult(False, "invalid_quantity")
    if (
        step is None
        or min_qty is None
        or min_notional is None
        or (metadata.max_notional is not None and max_notional is None)
        or not step.is_finite()
        or not min_qty.is_finite()
        or not min_notional.is_finite()
        or (max_notional is not None and not max_notional.is_finite())
        or step <= 0
        or min_qty < 0
        or min_notional < 0
        or (max_notional is not None and max_notional < 0)
    ):
        return VenueValidationResult(False, "invalid_metadata")
    if qty < min_qty:
        return VenueValidationResult(False, "below_min_qty")
    if not _is_multiple(qty, step):
        return VenueValidationResult(False, "invalid_step")

    tick = _dec(metadata.price_tick)
    if tick is None or not tick.is_finite() or tick <= 0:
        return VenueValidationResult(False, "invalid_metadata")
    price = _dec(intent.price)
    if price is not None and (not price.is_finite() or price <= 0):
        return VenueValidationResult(False, "invalid_price")
    if intent.order_type != "market":
        if price is None:
            return VenueValidationResult(False, "invalid_metadata")
        if not _is_multiple(price, tick):
            return VenueValidationResult(False, "invalid_tick")
    elif price is None:
        return VenueValidationResult(False, "missing_reference_price")

    stop_price = _dec(intent.stop_price)
    if intent.stop_price is not None and (
        stop_price is None or not stop_price.is_finite() or stop_price <= 0
    ):
        return VenueValidationResult(False, "invalid_stop_price")
    if stop_price is not None and not _is_multiple(stop_price, tick):
        return VenueValidationResult(False, "invalid_stop_tick")
    target_price = _dec(intent.target_price)
    if intent.target_price is not None and (
        target_price is None or not target_price.is_finite() or target_price <= 0
    ):
        return VenueValidationResult(False, "invalid_target_price")
    if target_price is not None and not _is_multiple(target_price, tick):
        return VenueValidationResult(False, "invalid_target_tick")

    if intent.role == "entry" and stop_price is not None:
        if (intent.side == "buy" and stop_price >= price) or (
            intent.side == "sell" and stop_price <= price
        ):
            return VenueValidationResult(False, "invalid_stop_side")
    if intent.role == "entry" and target_price is not None:
        if (intent.side == "buy" and target_price <= price) or (
            intent.side == "sell" and target_price >= price
        ):
            return VenueValidationResult(False, "invalid_target_side")

    notional = price * qty
    if notional < min_notional:
        return VenueValidationResult(False, "below_min_notional")
    if max_notional is not None and notional > max_notional:
        return VenueValidationResult(False, "above_max_notional")

    return VenueValidationResult(
        True,
        normalized_price=None if intent.price is None else _format(price, metadata.price_precision),
        normalized_quantity=_format(qty, metadata.quantity_precision),
        normalized_stop_price=None
        if stop_price is None
        else _format(stop_price, metadata.price_precision),
        normalized_target_price=None
        if target_price is None
        else _format(target_price, metadata.price_precision),
    )
