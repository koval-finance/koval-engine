"""Normalized perpetual-funding evidence and deterministic cashflow arithmetic."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from koval.engine.market_identity import canonical_symbol, resolve_market_identity
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
    interval_ms: int | None = None
    settlement_anchor_ms: int | None = None
    schedule_source: str | None = None

    def __post_init__(self) -> None:
        _validate_series(self)

    def validate_execution_grid(self, interval_ms: int) -> None:
        """OHLCV matching cannot locate exposure at an interior settlement."""
        if any(record.settlement_timestamp_ms % interval_ms for record in self.records):
            raise ValueError(
                "funding settlements must align with the execution candle grid; "
                "use a finer execution timeframe"
            )


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


def _funding_symbol(exchange: str, symbol: str) -> str:
    if exchange == "whitebit":
        from koval.exchanges.whitebit import WhiteBITAdapter

        return canonical_symbol(WhiteBITAdapter(market="future")._market_symbol(symbol))
    return canonical_symbol(symbol)


def _validate_series(series: FundingSeries) -> None:
    identity = resolve_market_identity(
        exchange=series.exchange, market=series.market, symbol=series.canonical_symbol
    )
    if identity.market != "future":
        raise ValueError("funding is unavailable for spot markets")
    start = _non_negative_timestamp(series.requested_start_ms, field_name="requested_start_ms")
    end = _non_negative_timestamp(series.requested_end_ms, field_name="requested_end_ms")
    if end < start:
        raise ValueError("funding requested end must not precede start")
    records = series.records
    timestamps = [record.settlement_timestamp_ms for record in records]
    if timestamps != sorted(set(timestamps)):
        raise ValueError("duplicate or unordered funding settlement timestamp")
    if any(not start <= ts <= end for ts in timestamps):
        raise ValueError("funding record is outside requested coverage")
    if any(
        _funding_symbol(identity.exchange, r.symbol)
        != _funding_symbol(identity.exchange, identity.canonical_symbol)
        for r in records
    ):
        raise ValueError("funding record symbol does not match requested instrument")
    intervals = {record.interval_ms for record in records}
    if len(intervals) > 1:
        raise ValueError("funding interval must be one positive constant for the series")
    interval = series.interval_ms
    if interval is not None and (
        isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0
    ):
        raise ValueError("funding interval_ms must be a positive integer")
    if records:
        inferred = records[0].interval_ms
        if interval is not None and interval != inferred:
            raise ValueError("declared funding interval_ms disagrees with its records")
        interval = inferred
    if not series.coverage_complete:
        return
    if not records:
        if interval is None or end - start >= interval:
            raise ValueError("incomplete funding coverage: no settlement records")
        anchor = series.settlement_anchor_ms
        if anchor is None or not series.schedule_source:
            raise ValueError("empty funding coverage needs settlement anchor and schedule_source")
        _non_negative_timestamp(anchor, field_name="settlement_anchor_ms")
        next_settlement = start + (anchor - start) % interval
        if next_settlement <= end:
            raise ValueError("incomplete funding coverage: missing scheduled settlement")
        return
    if any(b - a != interval for a, b in zip(timestamps, timestamps[1:], strict=False)):
        raise ValueError("incomplete funding coverage: missing settlement")
    if timestamps[0] - start >= interval or end - timestamps[-1] >= interval:
        raise ValueError("incomplete funding coverage: missing boundary settlement")


def build_funding_series(
    records: list[FundingRecord] | tuple[FundingRecord, ...],
    *,
    exchange: str,
    market: str,
    symbol: str,
    requested_start_ms: int,
    requested_end_ms: int,
    interval_ms: int | None = None,
    settlement_anchor_ms: int | None = None,
    schedule_source: str | None = None,
    raw_responses: tuple[object, ...] = (),
) -> FundingSeries:
    """Validate inclusive settlement coverage, identity and schedule evidence.

    An empty window requires a sourced settlement anchor and interval proving
    that no settlement was scheduled there. Duration alone is not evidence.
    """
    return FundingSeries(
        exchange=str(exchange).strip().lower(),
        market=canonical_market(market),
        canonical_symbol=canonical_symbol(symbol),
        requested_start_ms=requested_start_ms,
        requested_end_ms=requested_end_ms,
        records=tuple(sorted(records, key=lambda record: record.settlement_timestamp_ms)),
        coverage_complete=True,
        raw_responses=tuple(raw_responses),
        interval_ms=interval_ms,
        settlement_anchor_ms=settlement_anchor_ms,
        schedule_source=schedule_source,
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
