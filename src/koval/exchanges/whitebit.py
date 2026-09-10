"""WhiteBIT v1 public-REST kline adapter (read-only).

OHLCV uses ``GET /api/v1/public/kline`` (v4 ``/api/v4/public/kline`` is not served).
This adapter exposes market data only; a WebSocket feed and authenticated
endpoints are not implemented.
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

_BASE_URL: Final = "https://whitebit.com"
_KLINE_PATH: Final = "/api/v1/public/kline"
_MAX_LIMIT: Final = 1440
_FUNDING_LIMIT: Final = 100
_MARKETS: Final[frozenset[str]] = frozenset({"spot", "future"})
# Quote suffixes stripped from a separator-less pair (e.g. ``BTCUSDT``) to
# recover the base asset for a ``<BASE>_PERP`` futures symbol. Longest first so
# ``USDT``/``USDC`` win over ``USD``; ``PERP`` keeps the translation idempotent.
_FUTURES_QUOTE_SUFFIXES: Final[tuple[str, ...]] = ("PERP", "USDT", "USDC", "USD")

_INTERVAL_MAP: Final[dict[str, str]] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


class WhiteBITAdapter(ExchangeAdapter):
    def __init__(
        self,
        *,
        market: str = "spot",
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
            raise ValueError(f"unsupported WhiteBIT market: {market!r}")
        self._market = market
        self.base_url = _BASE_URL
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
        step_s = timeframe_ms(timeframe) // 1000
        params_base = {
            "market": self._market_symbol(symbol),
            "interval": self._interval(timeframe),
            "limit": self._page_limit,
        }
        pages: list[np.ndarray] = []
        cursor_s = start_ms // 1000
        end_s = end_ms // 1000
        while cursor_s < end_s:
            params = {**params_base, "start": cursor_s, "end": end_s}
            resp = get_with_retries(
                self._session,
                f"{self.base_url}{_KLINE_PATH}",
                params=params,
                timeout=self._timeout,
                max_retries=self._max_retries,
                backoff_seconds=self._retry_backoff_seconds,
            )
            resp.raise_for_status()
            raw = _parse_kline_rows(resp)
            if not raw:
                break
            page = self._rows_to_ndarray(raw)
            pages.append(page)
            last_open_ms = int(np.max(page[:, 0]))
            next_cursor_s = last_open_ms // 1000 + step_s
            if next_cursor_s <= cursor_s:
                raise RuntimeError("WhiteBIT OHLCV pagination did not advance")
            cursor_s = next_cursor_s
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
            raise FundingUnavailableError("WhiteBIT spot has no perpetual funding")
        raw_pages: list[object] = []
        rows: list[dict[str, object]] = []
        offset = 0
        while True:
            response = get_with_retries(
                self._session,
                f"{self.base_url}/api/v4/public/funding-history/{self._market_symbol(symbol)}",
                params={
                    "startDate": int(start_ms) // 1000,
                    "endDate": int(end_ms) // 1000,
                    "limit": _FUNDING_LIMIT,
                    "offset": offset,
                },
                timeout=self._timeout,
                max_retries=self._max_retries,
                backoff_seconds=self._retry_backoff_seconds,
            )
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, list):
                raise ValueError("WhiteBIT funding history response must be a list")
            raw_pages.append(raw)
            page = [item for item in raw if isinstance(item, dict)]
            rows.extend(page)
            if len(raw) < _FUNDING_LIMIT:
                break
            offset += len(raw)
        timestamps = sorted(int(item["fundingTime"]) * 1000 for item in rows)
        if len(timestamps) > 1:
            interval = timestamps[1] - timestamps[0]
        elif rows:
            interval = (int(rows[0]["fundingTime"]) - int(rows[0]["rateCalculatedTime"])) * 1000
        else:
            interval = 0
        records = [
            FundingRecord(
                symbol=str(item["market"]),
                rate=Decimal(str(item["fundingRate"])),
                settlement_timestamp_ms=int(item["fundingTime"]) * 1000,
                settlement_mark_price=Decimal(str(item["settlementPrice"])),
                interval_ms=interval,
                source="whitebit_v4_funding_history",
                rate_calculated_timestamp_ms=int(item["rateCalculatedTime"]) * 1000,
            )
            for item in rows
        ]
        return build_funding_series(
            records,
            exchange="whitebit",
            market="future",
            symbol=symbol,
            requested_start_ms=start_ms,
            requested_end_ms=end_ms,
            raw_responses=tuple(raw_pages),
        )

    @staticmethod
    def _rows_to_ndarray(raw: list[list]) -> np.ndarray:
        # Source columns:  [time_s, open, close, high, low, volume, quote_volume]
        # Koval columns:   [ts_ms,  open, high,  low,  close, volume]
        out = np.empty((len(raw), len(OHLCV_COLUMNS)), dtype=np.float64)
        for i, row in enumerate(raw):
            out[i, 0] = float(row[0]) * 1000.0  # s -> ms
            out[i, 1] = float(row[1])  # open
            out[i, 2] = float(row[3])  # high
            out[i, 3] = float(row[4])  # low
            out[i, 4] = float(row[2])  # close
            out[i, 5] = float(row[5])  # volume
        return out

    def capabilities(self) -> VenueCapabilities:
        return VenueCapabilities(
            exchange="whitebit",
            market=self._market,
            data_environment="production",
            # No verified non-money WhiteBIT endpoint exists, so execution
            # stays fail-closed regardless of the market.
            sandbox_execution=False,
            entry_order_types=(),
            take_profit_order_type="unavailable",
            fee_schedule="current_snapshot",
            funding_history="historical" if self._market == "future" else "unavailable",
            symbol_spec="current_snapshot",
            symbol_format="BTC_PERP" if self._market == "future" else "BTC_USDT",
        )

    def metadata(self) -> dict[str, Any]:
        symbols = (
            ["BTC_PERP", "ETH_PERP", "SOL_PERP"]
            if self._market == "future"
            else ["BTC_USDT", "ETH_USDT", "SOL_USDT"]
        )
        return {
            "name": "whitebit",
            "symbols": symbols,
            "timeframes": list(TIMEFRAMES),
        }

    def _market_symbol(self, symbol: str) -> str:
        """Map a canonical pair onto the market's own symbol spelling.

        Spot keeps the ``<BASE>_<QUOTE>`` form. Futures always use ``<BASE>_PERP``:
        a separator-less pair such as ``BTCUSDT`` has its quote suffix stripped
        rather than being sent to the venue verbatim, and an unparseable symbol
        raises instead of querying the wrong market.
        """
        normalized = self._normalize_symbol(symbol)
        if self._market != "future":
            return normalized
        if "_" in normalized:
            return f"{normalized.partition('_')[0]}_PERP"
        for suffix in _FUTURES_QUOTE_SUFFIXES:
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                return f"{normalized[: -len(suffix)]}_PERP"
        raise ValueError(
            f"cannot resolve WhiteBIT futures symbol from {symbol!r}: "
            "use '<BASE>_PERP', '<BASE>/<QUOTE>' or '<BASE><QUOTE>'"
        )

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return symbol.replace("/", "_").upper()

    @staticmethod
    def _interval(timeframe: str) -> str:
        try:
            return _INTERVAL_MAP[timeframe]
        except KeyError as exc:
            raise ValueError(f"unknown timeframe: {timeframe!r}") from exc


def _parse_kline_rows(resp: requests.Response) -> list:
    payload = resp.json()
    if not isinstance(payload, dict) or not payload.get("success"):
        message = payload.get("message") if isinstance(payload, dict) else payload
        raise requests.HTTPError(
            f"WhiteBIT kline failed: {message}",
            response=resp,
        )
    result = payload.get("result")
    return result if isinstance(result, list) else []
