"""Historical instrument constraints and mark-price liquidation primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation

from koval.exchanges.markets import canonical_market


def _positive_decimal(value: Decimal | str | float, *, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be positive and finite") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return parsed


@dataclass(frozen=True)
class MaintenanceMarginTier:
    notional_floor: Decimal
    notional_cap: Decimal | None
    maintenance_margin_rate: Decimal
    maintenance_amount: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        floor = Decimal(str(self.notional_floor))
        rate = Decimal(str(self.maintenance_margin_rate))
        amount = Decimal(str(self.maintenance_amount))
        if not floor.is_finite() or floor < 0:
            raise ValueError("margin-tier notional floor must be non-negative")
        if self.notional_cap is not None:
            cap = _positive_decimal(self.notional_cap, name="margin-tier notional cap")
            if cap <= floor:
                raise ValueError("margin-tier notional cap must exceed its floor")
        if not rate.is_finite() or rate < 0 or rate >= 1:
            raise ValueError("maintenance margin rate must be in [0, 1)")
        if not amount.is_finite() or amount < 0:
            raise ValueError("maintenance amount must be non-negative")


@dataclass(frozen=True)
class InstrumentSpecEvidence:
    evidence_id: str
    exchange: str
    market: str
    canonical_symbol: str
    effective_from_ms: int
    effective_to_ms: int | None
    tick_size: Decimal
    step_size: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    minimum_price: Decimal
    maximum_price: Decimal
    contract_size: Decimal
    collateral_currency: str
    margin_tiers: tuple[MaintenanceMarginTier, ...]
    liquidation_fee_bps: Decimal
    source: str
    evidence_status: str
    price_band_low_multiplier: Decimal | None = None
    price_band_high_multiplier: Decimal | None = None
    margin_mode: str = "cross"

    def __post_init__(self) -> None:
        if (
            not self.evidence_id
            or not self.exchange
            or not self.canonical_symbol
            or not self.source
            or not self.collateral_currency
        ):
            raise ValueError(
                "instrument evidence identity, exchange, symbol, source, and collateral are required"
            )
        if self.evidence_status not in {"historical", "current_snapshot", "approximation"}:
            raise ValueError("unsupported instrument evidence status")
        if canonical_market(self.market) != self.market:
            raise ValueError("instrument market must be canonical spot or future")
        if self.margin_mode != "cross":
            raise ValueError("instrument liquidation model currently supports cross margin only")
        for field_name, value in (
            ("effective_from_ms", self.effective_from_ms),
            ("effective_to_ms", self.effective_to_ms),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"instrument {field_name} must be a non-negative integer")
        for name in (
            "tick_size",
            "step_size",
            "minimum_quantity",
            "minimum_notional",
            "minimum_price",
            "maximum_price",
            "contract_size",
        ):
            _positive_decimal(getattr(self, name), name=f"instrument {name}")
        if Decimal(str(self.maximum_price)) <= Decimal(str(self.minimum_price)):
            raise ValueError("instrument maximum price must exceed minimum price")
        fee = Decimal(str(self.liquidation_fee_bps))
        if not fee.is_finite() or not Decimal("0") <= fee < Decimal("10000"):
            raise ValueError("liquidation fee must be finite and below one notional")
        if not self.margin_tiers:
            raise ValueError("at least one maintenance-margin tier is required")
        if Decimal(str(self.margin_tiers[0].notional_floor)) != 0:
            raise ValueError("margin tiers must start at zero notional")
        for previous, current in zip(self.margin_tiers, self.margin_tiers[1:], strict=False):
            previous_cap = previous.notional_cap
            current_floor = Decimal(str(current.notional_floor))
            if previous_cap is None or current_floor < Decimal(str(previous_cap)):
                raise ValueError("margin tiers must be sorted and non-overlapping")
        band_low = self.price_band_low_multiplier
        band_high = self.price_band_high_multiplier
        if (band_low is None) != (band_high is None):
            raise ValueError("instrument price-band multipliers must be supplied together")
        if band_low is not None and band_high is not None:
            low = _positive_decimal(band_low, name="price-band low multiplier")
            high = _positive_decimal(band_high, name="price-band high multiplier")
            if low >= high:
                raise ValueError("price-band low multiplier must be below the high multiplier")
        if self.effective_to_ms is not None and self.effective_to_ms < self.effective_from_ms:
            raise ValueError("instrument evidence interval is invalid")


@dataclass(frozen=True)
class NormalizedOrder:
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal
    notional: Decimal
    spec_evidence_id: str


@dataclass(frozen=True)
class MarkPriceRecord:
    timestamp_ms: int
    price: Decimal
    source: str

    def __post_init__(self) -> None:
        if int(self.timestamp_ms) != self.timestamp_ms or self.timestamp_ms < 0:
            raise ValueError("mark-price timestamp must be a non-negative integer")
        _positive_decimal(self.price, name="mark price")
        if not self.source:
            raise ValueError("mark-price source is required")


@dataclass(frozen=True)
class MarkPriceSeries:
    exchange: str
    canonical_symbol: str
    interval_ms: int
    requested_start_ms: int
    requested_end_ms: int
    records: tuple[MarkPriceRecord, ...]
    coverage_complete: bool
    raw_responses: tuple[object, ...] = field(default_factory=tuple)
    _by_timestamp: dict[int, MarkPriceRecord] | None = field(
        default=None, init=False, compare=False, repr=False
    )

    def at(self, timestamp_ms: int) -> MarkPriceRecord | None:
        """The mark price stamped exactly ``timestamp_ms``, or ``None``.

        Indexed on first use: the paper broker asks once per bar for the whole
        life of a position, so a scan here would make a run quadratic in its
        own length.
        """
        index = self._by_timestamp
        if index is None:
            index = {record.timestamp_ms: record for record in self.records}
            object.__setattr__(self, "_by_timestamp", index)
        return index.get(int(timestamp_ms))


@dataclass(frozen=True)
class LiquidationState:
    mark_price: Decimal
    notional: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    maintenance_margin: Decimal
    margin_tier: MaintenanceMarginTier
    liquidated: bool


def select_instrument_spec(
    specs: tuple[InstrumentSpecEvidence, ...] | list[InstrumentSpecEvidence],
    *,
    timestamp_ms: int,
) -> InstrumentSpecEvidence:
    timestamp = int(timestamp_ms)
    matches = [
        spec
        for spec in specs
        if spec.effective_from_ms <= timestamp
        and (spec.effective_to_ms is None or timestamp <= spec.effective_to_ms)
    ]
    if len(matches) != 1:
        reason = "ambiguous" if matches else "no"
        raise ValueError(f"{reason} instrument evidence for timestamp {timestamp}")
    return matches[0]


def _quantize(value: Decimal, step: Decimal, *, rounding: str) -> Decimal:
    units = (value / step).to_integral_value(rounding=rounding)
    return units * step


def normalize_order(
    spec: InstrumentSpecEvidence,
    *,
    side: str,
    order_type: str,
    quantity: Decimal,
    price: Decimal,
    reference_price: Decimal | None = None,
) -> NormalizedOrder:
    if side not in {"buy", "sell"}:
        raise ValueError("order side must be buy or sell")
    raw_quantity = _positive_decimal(quantity, name="order quantity")
    raw_price = _positive_decimal(price, name="order price")
    normalized_quantity = _quantize(raw_quantity, Decimal(str(spec.step_size)), rounding=ROUND_DOWN)
    if order_type in {"limit", "limit_at_zone"}:
        rounding = ROUND_DOWN if side == "buy" else ROUND_UP
    else:
        rounding = ROUND_UP if side == "buy" else ROUND_DOWN
    normalized_price = _quantize(raw_price, Decimal(str(spec.tick_size)), rounding=rounding)
    if normalized_quantity < Decimal(str(spec.minimum_quantity)):
        raise ValueError("order is below the instrument minimum quantity")
    if not Decimal(str(spec.minimum_price)) <= normalized_price <= Decimal(str(spec.maximum_price)):
        raise ValueError("order price is outside instrument price bounds")
    if reference_price is not None and spec.price_band_low_multiplier is not None:
        reference = _positive_decimal(reference_price, name="price-band reference price")
        low = reference * Decimal(str(spec.price_band_low_multiplier))
        high = reference * Decimal(str(spec.price_band_high_multiplier))
        if not low <= normalized_price <= high:
            raise ValueError("order price is outside the historical reference price band")
    notional = normalized_quantity * normalized_price * Decimal(str(spec.contract_size))
    if notional < Decimal(str(spec.minimum_notional)):
        raise ValueError("order is below the instrument minimum notional")
    return NormalizedOrder(
        side=side,
        order_type=order_type,
        quantity=normalized_quantity,
        price=normalized_price,
        notional=notional,
        spec_evidence_id=spec.evidence_id,
    )


def build_mark_price_series(
    records: tuple[MarkPriceRecord, ...] | list[MarkPriceRecord],
    *,
    exchange: str,
    symbol: str,
    interval_ms: int,
    requested_start_ms: int,
    requested_end_ms: int,
    raw_responses: tuple[object, ...] = (),
) -> MarkPriceSeries:
    interval = int(interval_ms)
    if interval <= 0:
        raise ValueError("mark-price interval must be positive")
    ordered = tuple(sorted(records, key=lambda record: record.timestamp_ms))
    timestamps = [record.timestamp_ms for record in ordered]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate mark-price timestamp")
    expected = list(range(int(requested_start_ms), int(requested_end_ms) + 1, interval))
    if timestamps != expected:
        missing = next((timestamp for timestamp in expected if timestamp not in timestamps), None)
        raise ValueError(f"incomplete mark-price coverage: missing mark price {missing}")
    return MarkPriceSeries(
        exchange=str(exchange).strip().lower(),
        canonical_symbol=str(symbol).replace("/", "").replace("_", "").upper(),
        interval_ms=interval,
        requested_start_ms=int(requested_start_ms),
        requested_end_ms=int(requested_end_ms),
        records=ordered,
        coverage_complete=True,
        raw_responses=tuple(raw_responses),
    )


def evaluate_liquidation(
    spec: InstrumentSpecEvidence,
    *,
    side: str,
    quantity: Decimal,
    entry_price: Decimal,
    cash_balance: Decimal,
    mark_price: Decimal,
) -> LiquidationState:
    if side not in {"buy", "sell"}:
        raise ValueError("position side must be buy or sell")
    qty = _positive_decimal(quantity, name="position quantity")
    entry = _positive_decimal(entry_price, name="position entry price")
    mark = _positive_decimal(mark_price, name="mark price")
    contract_size = Decimal(str(spec.contract_size))
    notional = qty * mark * contract_size
    tier = next(
        (
            candidate
            for candidate in spec.margin_tiers
            if notional >= Decimal(str(candidate.notional_floor))
            and (candidate.notional_cap is None or notional <= Decimal(str(candidate.notional_cap)))
        ),
        None,
    )
    if tier is None:
        raise ValueError("no maintenance-margin tier covers position notional")
    sign = Decimal("1") if side == "buy" else Decimal("-1")
    unrealized = (mark - entry) * qty * contract_size * sign
    equity = Decimal(str(cash_balance)) + unrealized
    maintenance = max(
        Decimal("0"),
        notional * Decimal(str(tier.maintenance_margin_rate))
        - Decimal(str(tier.maintenance_amount)),
    )
    return LiquidationState(
        mark_price=mark,
        notional=notional,
        unrealized_pnl=unrealized,
        equity=equity,
        maintenance_margin=maintenance,
        margin_tier=tier,
        liquidated=equity <= maintenance,
    )


__all__ = [
    "InstrumentSpecEvidence",
    "LiquidationState",
    "MaintenanceMarginTier",
    "MarkPriceRecord",
    "MarkPriceSeries",
    "NormalizedOrder",
    "build_mark_price_series",
    "evaluate_liquidation",
    "normalize_order",
    "select_instrument_spec",
]
