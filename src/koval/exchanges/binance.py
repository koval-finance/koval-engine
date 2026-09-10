"""Binance Futures REST adapter (read-only).

This adapter exposes ``fetch_ohlcv`` only. Order placement, balances, and
positions are the sandbox broker's responsibility, not the data adapter's.
"""

from __future__ import annotations

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
_MARKETS: Final[frozenset[str]] = frozenset({"spot", "future"})

_INTERVAL_MAP: Final[dict[str, str]] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


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
        cursor = int(start_ms)
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
        interval = timestamps[1] - timestamps[0]
        records = [
            FundingRecord(
                symbol=str(item["symbol"]),
                rate=Decimal(str(item["fundingRate"])),
                settlement_timestamp_ms=int(item["fundingTime"]),
                settlement_mark_price=Decimal(str(item["markPrice"])),
                interval_ms=interval,
                source="binance_usdm_funding_rate",
            )
            for item in rows
        ]
        return build_funding_series(
            records,
            exchange="binance",
            market="future",
            symbol=symbol,
            requested_start_ms=start_ms,
            requested_end_ms=end_ms,
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
