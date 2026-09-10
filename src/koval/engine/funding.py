"""Normalized perpetual-funding evidence and deterministic cashflow arithmetic."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from koval.exchanges.markets import canonical_market


class FundingUnavailableError(RuntimeError):
    """Raised when the selected venue/market has no supported funding evidence."""


@dataclass(frozen=True)
class FundingRecord:
    symbol: str
    rate: Decimal
    settlement_timestamp_ms: int
    settlement_mark_price: Decimal
    interval_ms: int
    source: str
    rate_calculated_timestamp_ms: int | None = None

    def __post_init__(self) -> None:
        if not str(self.symbol).strip() or not str(self.source).strip():
            raise ValueError("funding symbol and source are required")
        _decimal(self.rate, field_name="rate")
        _decimal(
            self.settlement_mark_price,
            field_name="settlement mark price",
            positive=True,
        )
        _non_negative_timestamp(
            self.settlement_timestamp_ms,
            field_name="settlement_timestamp_ms",
        )
        if (
            isinstance(self.interval_ms, bool)
            or not isinstance(self.interval_ms, int)
            or self.interval_ms <= 0
        ):
            raise ValueError("funding interval_ms must be a positive integer")
        calculated = self.rate_calculated_timestamp_ms
        if calculated is not None:
            _non_negative_timestamp(
                calculated,
                field_name="rate_calculated_timestamp_ms",
            )
            if calculated > self.settlement_timestamp_ms:
                raise ValueError("funding rate_calculated_timestamp_ms must not follow settlement")


@dataclass(frozen=True)
class FundingSeries:
    exchange: str
    market: str
    canonical_symbol: str
    requested_start_ms: int
    requested_end_ms: int
    records: tuple[FundingRecord, ...]
    coverage_complete: bool
    raw_responses: tuple[object, ...] = field(default_factory=tuple)


def _decimal(value: object, *, field_name: str, positive: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"funding {field_name} must be finite") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise ValueError(f"funding {field_name} must be finite")
    return parsed


def _non_negative_timestamp(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"funding {field_name} must be a non-negative integer")
    return value


def build_funding_series(
    records: list[FundingRecord] | tuple[FundingRecord, ...],
    *,
    exchange: str,
    market: str,
    symbol: str,
    requested_start_ms: int,
    requested_end_ms: int,
    interval_ms: int | None = None,
    raw_responses: tuple[object, ...] = (),
) -> FundingSeries:
    """Validate that every settlement inside the requested window is present.

    Coverage is judged against the phase the records themselves reveal, not an
    assumed epoch-aligned grid: venues settle on their own schedule and one
    that is offset from a multiple of the interval is still complete. Supply
    ``interval_ms`` when ``records`` is empty, which is only accepted for a
    window narrower than one interval — a window that spans a whole interval
    must contain a settlement, so an empty one is missing evidence.
    """
    canonical = canonical_market(market)
    if canonical != "future":
        raise ValueError("funding is unavailable for spot markets")
    start = int(requested_start_ms)
    end = int(requested_end_ms)
    if end < start:
        raise ValueError("funding requested end must not precede start")
    ordered = tuple(sorted(records, key=lambda record: record.settlement_timestamp_ms))
    timestamps = [record.settlement_timestamp_ms for record in ordered]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate funding settlement timestamp")
    intervals = {record.interval_ms for record in ordered}
    if len(intervals) > 1:
        raise ValueError("funding interval must be one positive constant for the series")
    if interval_ms is not None and (
        isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or interval_ms <= 0
    ):
        raise ValueError("funding interval_ms must be a positive integer")
    interval = next(iter(intervals), None)
    if interval is None:
        if interval_ms is None:
            raise ValueError("an empty funding series must declare its interval_ms")
        interval = interval_ms
    elif interval_ms is not None and interval_ms != interval:
        raise ValueError("declared funding interval_ms disagrees with its records")
    for record in ordered:
        _decimal(record.rate, field_name="rate")
        _decimal(record.settlement_mark_price, field_name="settlement mark price", positive=True)
    if not ordered:
        if end - start >= interval:
            raise ValueError("incomplete funding coverage: no settlement records")
    else:
        expected = list(range(timestamps[0], timestamps[-1] + 1, interval))
        if timestamps != expected:
            observed = set(timestamps)
            missing = next((value for value in expected if value not in observed), None)
            raise ValueError(f"incomplete funding coverage: missing settlement {missing}")
        if timestamps[0] - start >= interval:
            raise ValueError(
                f"incomplete funding coverage: missing settlement {timestamps[0] - interval}"
            )
        if end - timestamps[-1] >= interval:
            raise ValueError(
                f"incomplete funding coverage: missing settlement {timestamps[-1] + interval}"
            )
    return FundingSeries(
        exchange=str(exchange).strip().lower(),
        market=canonical,
        canonical_symbol=str(symbol).replace("/", "").replace("_", "").upper(),
        requested_start_ms=int(requested_start_ms),
        requested_end_ms=int(requested_end_ms),
        records=ordered,
        coverage_complete=True,
        raw_responses=tuple(raw_responses),
    )


def funding_cashflow(
    *, side: str, quantity: Decimal, rate: Decimal, mark_price: Decimal
) -> Decimal:
    if side not in {"buy", "sell"}:
        raise ValueError("funding side must be buy or sell")
    parsed_quantity = _decimal(quantity, field_name="quantity", positive=True)
    parsed_rate = _decimal(rate, field_name="rate")
    parsed_mark = _decimal(mark_price, field_name="mark price", positive=True)
    position_sign = Decimal("1") if side == "buy" else Decimal("-1")
    return -(position_sign * parsed_quantity * parsed_rate * parsed_mark)


__all__ = [
    "FundingRecord",
    "FundingSeries",
    "FundingUnavailableError",
    "build_funding_series",
    "funding_cashflow",
]
