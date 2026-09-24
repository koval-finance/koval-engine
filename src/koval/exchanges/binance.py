"""Binance Futures REST adapter (read-only).

This adapter exposes public read-only market data only. Order placement,
balances, and positions are the sandbox broker's responsibility, not the data
adapter's.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import numpy as np
import requests

from koval.exchanges.base import (
    OHLCV_COLUMNS,
    TIMEFRAMES,
    ExchangeAdapter,
    get_with_retries,
    normalize_ohlcv_range,
    timeframe_ms,
    validate_transport_settings,
)
from koval.exchanges.capabilities import VenueCapabilities

_TESTNET_URL: Final = "https://testnet.binancefuture.com"
_PROD_URL: Final = "https://fapi.binance.com"
_SPOT_URL: Final = "https://api.binance.com"
_KLINES_PATH: Final = "/fapi/v1/klines"
_SPOT_KLINES_PATH: Final = "/api/v3/klines"
_MAX_LIMIT: Final = 1500
_FUNDING_LIMIT: Final = 1000
_FUNDING_LOOKBACK_MS: Final = 3 * 24 * 60 * 60 * 1000
_AGGREGATE_TRADE_LIMIT: Final = 1000
_AGGREGATE_TRADE_MAX_WINDOW_MS: Final = 60 * 60 * 1000
_DEPTH_LIMITS: Final = frozenset({5, 10, 20, 50, 100, 500, 1000})
_MARKETS: Final[frozenset[str]] = frozenset({"spot", "future"})

_INTERVAL_MAP: Final[dict[str, str]] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


@dataclass(frozen=True)
class AggregateTradeRecord:
    aggregate_trade_id: int
    timestamp_ms: int
    price: Decimal
    quantity: Decimal
    buyer_is_maker: bool


@dataclass(frozen=True)
class AggregateTradeEvidence:
    records: tuple[AggregateTradeRecord, ...]
    coverage_start_ms: int
    coverage_end_ms: int
    raw_responses: tuple[object, ...]


@dataclass(frozen=True)
class OrderBookSnapshotEvidence:
    snapshot: Any
    observed_at_ms: int
    raw_response: dict[str, object]


class BinanceAdapter(ExchangeAdapter):
    def __init__(
        self,
        *,
        testnet: bool = True,
        market: str = "future",
        session: requests.Session | None = None,
        timeout: float = 10.0,
        page_limit: int = _MAX_LIMIT,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.25,
    ) -> None:
        timeout, page_limit, max_retries, retry_backoff_seconds = validate_transport_settings(
            timeout=timeout,
            page_limit=page_limit,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        if market not in _MARKETS:
            raise ValueError(f"unsupported Binance market: {market!r}")
        self._market = market
        if market == "spot":
            # Spot klines live on a different host and path; the first six
            # fields of a row have the same layout as the futures response.
            if testnet:
                raise ValueError("Binance spot data is served from production only")
            self.base_url = _SPOT_URL
            self._klines_path = _SPOT_KLINES_PATH
        else:
            self.base_url = _TESTNET_URL if testnet else _PROD_URL
            self._klines_path = _KLINES_PATH
        self._testnet = testnet
        self._session = session or requests.Session()
        self._timeout = timeout
        self._page_limit = page_limit
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> np.ndarray:
        if start_ms >= end_ms:
            return np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
        step = timeframe_ms(timeframe)
        params_base = {
            "symbol": self._normalize_symbol(symbol),
            "interval": self._interval(timeframe),
            "limit": self._page_limit,
        }
        pages: list[np.ndarray] = []
        cursor = start_ms
        while cursor < end_ms:
            params = {**params_base, "startTime": cursor, "endTime": end_ms}
            resp = get_with_retries(
                self._session,
                f"{self.base_url}{self._klines_path}",
                params=params,
                timeout=self._timeout,
                max_retries=self._max_retries,
                backoff_seconds=self._retry_backoff_seconds,
            )
            resp.raise_for_status()
            raw = resp.json()
            if not raw:
                break
            page = self._rows_to_ndarray(raw)
            pages.append(page)
            last_open = int(np.max(page[:, 0]))
            next_cursor = last_open + step
            if next_cursor <= cursor:
                raise RuntimeError("Binance OHLCV pagination did not advance")
            cursor = next_cursor
            if len(raw) < self._page_limit:
                break
        if not pages:
            return np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
        return normalize_ohlcv_range(
            np.vstack(pages),
            start_ms=start_ms,
            end_ms=end_ms,
        )

    def fetch_mark_prices(self, symbol: str, timeframe: str, start_ms: int, end_ms: int):
        """Historical mark samples at candle opens, inclusive of both bounds.

        Candle high/low/close are deliberately not substituted for the mark
        known at its opening timestamp. This is a sampled liquidation model.
        """
        from koval.engine.instrument_risk import MarkPriceRecord, build_mark_price_series

        if self._market != "future":
            raise ValueError("mark-price evidence is unavailable for spot")
        step = timeframe_ms(timeframe)
        if start_ms < 0 or end_ms < start_ms or start_ms % step or end_ms % step:
            raise ValueError("mark-price bounds must be aligned and ordered")
        records, pages = [], []
        for cursor in range(start_ms, end_ms + 1, self._page_limit * step):
            page_end = min(cursor + (self._page_limit - 1) * step, end_ms)
            response = get_with_retries(
                self._session,
                f"{self.base_url}/fapi/v1/markPriceKlines",
                params={
                    "symbol": self._normalize_symbol(symbol),
                    "interval": timeframe,
                    "startTime": cursor,
                    "endTime": page_end,
                    "limit": self._page_limit,
                },
                timeout=self._timeout,
                max_retries=self._max_retries,
                backoff_seconds=self._retry_backoff_seconds,
            )
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, list):
                raise ValueError("Binance mark-price response must be a list")
            pages.append(raw)
            page = [
                MarkPriceRecord(
                    int(row[0]), Decimal(str(row[1])), "binance_usdm_mark_price_kline_open"
                )
                for row in raw
            ]
            if [row.timestamp_ms for row in page] != list(range(cursor, page_end + 1, step)):
                raise ValueError(
                    "incomplete mark-price coverage: repeated, truncated or missing page"
                )
            records.extend(page)
        return build_mark_price_series(
            records,
            exchange="binance",
            symbol=symbol,
            interval_ms=step,
            requested_start_ms=start_ms,
            requested_end_ms=end_ms,
            raw_responses=tuple(pages),
        )

    def fetch_aggregate_trades(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> AggregateTradeEvidence:
        """Return one complete, native-ID-verified aggregate-trade window."""
        if self._market != "future":
            raise ValueError("aggregate-trade evidence is Binance Futures only")
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("aggregate-trade bounds must be non-negative and ordered")
        pages: list[object] = []
        records: list[AggregateTradeRecord] = []
        cursor = int(start_ms)
        while cursor <= end_ms:
            window_end = min(cursor + _AGGREGATE_TRADE_MAX_WINDOW_MS - 1, int(end_ms))
            params: dict[str, object] = {
                "symbol": self._normalize_symbol(symbol),
                "startTime": cursor,
                "endTime": window_end,
                "limit": _AGGREGATE_TRADE_LIMIT,
            }
            while True:
                response = get_with_retries(
                    self._session,
                    f"{self.base_url}/fapi/v1/aggTrades",
                    params=params,
                    timeout=self._timeout,
                    max_retries=self._max_retries,
                    backoff_seconds=self._retry_backoff_seconds,
                )
                response.raise_for_status()
                raw = response.json()
                if not isinstance(raw, list):
                    raise ValueError("Binance aggregate-trade response must be a list")
                pages.append(raw)
                page = [_aggregate_trade_record(item) for item in raw]
                records.extend(
                    record for record in page if int(start_ms) <= record.timestamp_ms <= int(end_ms)
                )
                if (
                    len(raw) < _AGGREGATE_TRADE_LIMIT
                    or not page
                    or page[-1].timestamp_ms > window_end
                ):
                    break
                params = {
                    "symbol": self._normalize_symbol(symbol),
                    "fromId": page[-1].aggregate_trade_id + 1,
                    "limit": _AGGREGATE_TRADE_LIMIT,
                }
            cursor = window_end + 1
        selected = tuple(
            sorted(
                {record.aggregate_trade_id: record for record in records}.values(),
                key=lambda record: record.aggregate_trade_id,
            )
        )
        for left, right in zip(selected, selected[1:], strict=False):
            if right.aggregate_trade_id != left.aggregate_trade_id + 1:
                raise ValueError("Binance aggregate-trade sequence gap")
            if right.timestamp_ms < left.timestamp_ms:
                raise ValueError("Binance aggregate-trade timestamp regression")
        return AggregateTradeEvidence(
            records=selected,
            coverage_start_ms=int(start_ms),
            coverage_end_ms=int(end_ms),
            raw_responses=tuple(pages),
        )

    def fetch_order_book_snapshot(
        self,
        symbol: str,
        *,
        limit: int = 100,
    ) -> OrderBookSnapshotEvidence:
        """Acquire a current L2 snapshot without implying historical coverage."""
        from koval.engine.market_depth_execution import BookLevel, OrderBookSnapshot

        if self._market != "future":
            raise ValueError("order-book evidence is Binance Futures only")
        if isinstance(limit, bool) or limit not in _DEPTH_LIMITS:
            raise ValueError("unsupported Binance Futures depth limit")
        response = get_with_retries(
            self._session,
            f"{self.base_url}/fapi/v1/depth",
            params={"symbol": self._normalize_symbol(symbol), "limit": limit},
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_seconds=self._retry_backoff_seconds,
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, dict):
            raise ValueError("Binance order-book response must be an object")
        try:
            observed_at_ms = int(raw["E"])
            timestamp_ms = int(raw.get("T", observed_at_ms))
            sequence = int(raw["lastUpdateId"])
            bids = tuple(
                BookLevel(Decimal(str(row[0])), Decimal(str(row[1]))) for row in raw["bids"]
            )
            asks = tuple(
                BookLevel(Decimal(str(row[0])), Decimal(str(row[1]))) for row in raw["asks"]
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ValueError("invalid Binance order-book snapshot") from exc
        return OrderBookSnapshotEvidence(
            snapshot=OrderBookSnapshot(
                timestamp_ms=timestamp_ms,
                source="binance_usdm_depth_snapshot",
                source_sequence=sequence,
                bids=bids,
                asks=asks,
            ),
            observed_at_ms=observed_at_ms,
            raw_response=raw,
        )

    def fetch_funding_history(self, symbol: str, start_ms: int, end_ms: int):
        from koval.engine.funding import (
            FundingRecord,
            FundingUnavailableError,
            build_funding_series,
        )

        if self._market != "future":
            raise FundingUnavailableError("Binance spot has no perpetual funding")
        raw_pages: list[object] = []
        rows: list[dict[str, object]] = []
        cursor = max(0, int(start_ms) - _FUNDING_LOOKBACK_MS)
        while cursor <= end_ms:
            response = get_with_retries(
                self._session,
                f"{self.base_url}/fapi/v1/fundingRate",
                params={
                    "symbol": self._normalize_symbol(symbol),
                    "startTime": cursor,
                    "endTime": int(end_ms),
                    "limit": _FUNDING_LIMIT,
                },
                timeout=self._timeout,
                max_retries=self._max_retries,
                backoff_seconds=self._retry_backoff_seconds,
            )
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, list):
                raise ValueError("Binance funding history response must be a list")
            raw_pages.append(raw)
            rows.extend(item for item in raw if isinstance(item, dict))
            if len(raw) < _FUNDING_LIMIT:
                break
            next_cursor = int(raw[-1]["fundingTime"]) + 1
            if next_cursor <= cursor:
                raise RuntimeError("Binance funding pagination did not advance")
            cursor = next_cursor
        timestamps = sorted(int(item["fundingTime"]) for item in rows)
        if len(timestamps) < 2:
            raise ValueError("Binance funding interval is unavailable from fewer than two records")
        intervals = {right - left for left, right in zip(timestamps, timestamps[1:], strict=False)}
        if len(intervals) != 1 or next(iter(intervals)) <= 0:
            raise ValueError("Binance funding interval changed inside the acquired evidence window")
        interval = next(iter(intervals))
        selected = [
            item for item in rows if int(start_ms) <= int(item["fundingTime"]) <= int(end_ms)
        ]
        records = [
            FundingRecord(
                symbol=str(item["symbol"]),
                rate=Decimal(str(item["fundingRate"])),
                settlement_timestamp_ms=int(item["fundingTime"]),
                settlement_mark_price=Decimal(str(item["markPrice"])),
                interval_ms=interval,
                source="binance_usdm_funding_rate",
            )
            for item in sorted(selected, key=lambda value: int(value["fundingTime"]))
        ]
        return build_funding_series(
            records,
            exchange="binance",
            market="future",
            symbol=symbol,
            requested_start_ms=start_ms,
            requested_end_ms=end_ms,
            interval_ms=interval,
            settlement_anchor_ms=timestamps[-1],
            schedule_source="binance_usdm_funding_rate_history",
            raw_responses=tuple(raw_pages),
        )

    @staticmethod
    def _rows_to_ndarray(raw: list[list]) -> np.ndarray:
        out = np.empty((len(raw), len(OHLCV_COLUMNS)), dtype=np.float64)
        for i, row in enumerate(raw):
            out[i, 0] = float(row[0])
            out[i, 1] = float(row[1])
            out[i, 2] = float(row[2])
            out[i, 3] = float(row[3])
            out[i, 4] = float(row[4])
            out[i, 5] = float(row[5])
        return out

    def capabilities(self) -> VenueCapabilities:
        return VenueCapabilities(
            exchange="binance",
            market=self._market,
            data_environment="testnet" if self._testnet else "production",
            sandbox_execution=self._market == "future",
            entry_order_types=("market", "limit", "stop"),
            take_profit_order_type="take_profit_market",
            fee_schedule="current_snapshot",
            funding_history="historical" if self._market == "future" else "unavailable",
            symbol_spec="current_snapshot",
            symbol_format="BTCUSDT",
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "name": "binance",
            "symbols": [
                "BTCUSDT",
                "ETHUSDT",
                "SOLUSDT",
                "BNBUSDT",
                "ADAUSDT",
                "XRPUSDT",
                "DOGEUSDT",
                "AVAXUSDT",
            ],
            "timeframes": list(TIMEFRAMES),
        }

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return symbol.replace("/", "").replace("_", "").upper()

    @staticmethod
    def _interval(timeframe: str) -> str:
        try:
            return _INTERVAL_MAP[timeframe]
        except KeyError as exc:
            raise ValueError(f"unknown timeframe: {timeframe!r}") from exc


def _aggregate_trade_record(value: object) -> AggregateTradeRecord:
    if not isinstance(value, dict):
        raise ValueError("invalid Binance aggregate-trade record")
    if not isinstance(value.get("m"), bool):
        raise ValueError("invalid Binance aggregate-trade record")
    try:
        record = AggregateTradeRecord(
            aggregate_trade_id=int(value["a"]),
            timestamp_ms=int(value["T"]),
            price=Decimal(str(value["p"])),
            quantity=Decimal(str(value["q"])),
            buyer_is_maker=value["m"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid Binance aggregate-trade record") from exc
    if (
        record.aggregate_trade_id < 0
        or record.timestamp_ms < 0
        or not record.price.is_finite()
        or not record.quantity.is_finite()
        or record.price <= 0
        or record.quantity <= 0
    ):
        raise ValueError("invalid Binance aggregate-trade record")
    return record
