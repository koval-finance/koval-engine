"""Binance Futures REST adapter (read-only).

This adapter exposes ``fetch_ohlcv`` only. Order placement, balances, and
positions are the sandbox broker's responsibility, not the data adapter's.
"""

from __future__ import annotations

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

_TESTNET_URL: Final = "https://testnet.binancefuture.com"
_PROD_URL: Final = "https://fapi.binance.com"
_KLINES_PATH: Final = "/fapi/v1/klines"
_MAX_LIMIT: Final = 1500

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
        self.base_url = _TESTNET_URL if testnet else _PROD_URL
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
                f"{self.base_url}{_KLINES_PATH}",
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
