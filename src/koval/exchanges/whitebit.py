"""WhiteBIT v1 public-REST kline adapter (read-only).

OHLCV uses ``GET /api/v1/public/kline`` (v4 ``/api/v4/public/kline`` is not served).
This adapter exposes market data only; a WebSocket feed and authenticated
endpoints are not implemented.
"""

from __future__ import annotations

import time
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
        self._page_limit = min(page_limit, _MAX_LIMIT)
        self.last_fetch_metadata: dict[str, object] = {}
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> np.ndarray:
        step = timeframe_ms(timeframe)
        # WhiteBIT returns only closed candles and may return its default page
        # for a sub-interval request. Never query that forming-candle tail.
        cutoff = min(end_ms, int(time.time() * 1000)) // step * step
        first = (start_ms + step - 1) // step * step
        self.last_fetch_metadata = {
            "requested_start_ms": start_ms,
            "requested_end_ms": end_ms,
            "closed_cutoff_ms": cutoff,
            "effective_start_ms": first,
            "coverage_complete": False,
            "actual_start_ms": None,
            "actual_end_ms": None,
            "row_count": 0,
        }
        pages: list[np.ndarray] = []
        # Bound both ends. Advancing start alone can lose the beginning of a
        # range when the venue returns the last `limit` rows of that range.
        for cursor in range(first, cutoff, self._page_limit * step):
            page_end = min(cursor + self._page_limit * step, cutoff)
            resp = get_with_retries(
                self._session,
                f"{self.base_url}{_KLINE_PATH}",
                params={
                    "market": self._market_symbol(symbol),
                    "interval": self._interval(timeframe),
                    "limit": self._page_limit,
                    "start": cursor // 1000,
                    "end": page_end // 1000,
                },
                timeout=self._timeout,
                max_retries=self._max_retries,
                backoff_seconds=self._retry_backoff_seconds,
            )
            resp.raise_for_status()
            raw = _parse_kline_rows(resp)
            if not raw:
                if not pages and page_end == cutoff:
                    return np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
                raise RuntimeError("WhiteBIT incomplete OHLCV coverage: empty page")
            page = self._rows_to_ndarray(raw)
            selected = page[(page[:, 0] >= cursor) & (page[:, 0] < page_end)]
            if not len(selected):
                raise RuntimeError("WhiteBIT OHLCV pagination did not advance")
            for timestamp in np.unique(selected[:, 0]):
                duplicates = selected[selected[:, 0] == timestamp]
                if not np.all(duplicates == duplicates[0]):
                    raise ValueError("WhiteBIT conflicting OHLCV duplicate")
            selected = normalize_ohlcv_range(selected, start_ms=cursor, end_ms=page_end)
            expected = np.arange(cursor, page_end, step)
            if not np.array_equal(selected[:, 0], expected):
                raise RuntimeError("WhiteBIT incomplete OHLCV coverage: truncated page or gap")
            pages.append(selected)
        rows = np.vstack(pages) if pages else np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
        self.last_fetch_metadata.update(
            coverage_complete=True,
            actual_start_ms=int(rows[0, 0]) if len(rows) else None,
            actual_end_ms=int(rows[-1, 0]) + step if len(rows) else None,
            row_count=len(rows),
        )
        return rows

    def fetch_fee_schedule(self, symbol: str):
        """Current public default fees; never historical account-specific rates."""
        from koval.engine.fee_evidence import FeeScheduleEvidence
        from koval.engine.market_identity import canonical_symbol
        from koval.engine.run_identity import content_sha256

        response = get_with_retries(
            self._session,
            f"{self.base_url}/api/v4/public/markets",
            params={},
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_seconds=self._retry_backoff_seconds,
        )
        response.raise_for_status()
        raw = response.json()
        venue_symbol = self._market_symbol(symbol)
        matches = [
            item for item in raw if isinstance(item, dict) and item.get("name") == venue_symbol
        ]
        if len(matches) != 1:
            raise ValueError("WhiteBIT fee evidence requires one matching market")
        item = matches[0]
        expected_type = "futures" if self._market == "future" else "spot"
        if item.get("type") != expected_type:
            raise ValueError("WhiteBIT fee evidence market mismatch")
        observed = time.time_ns() // 1_000_000
        return FeeScheduleEvidence(
            evidence_id=content_sha256({"response": raw, "observed_ms": observed}),
            maker_bps=float(Decimal(str(item["makerFee"])) * 100),
            taker_bps=float(Decimal(str(item["takerFee"])) * 100),
            currency=str(item["money"]),
            evidence_status="current_snapshot",
            source="whitebit_v4_public_markets",
            effective_from_ms=observed,
            effective_to_ms=observed,
            discount_treatment="not_observed",
            tier_id="public_default",
            exchange="whitebit",
            market=self._market,
            canonical_symbol=canonical_symbol(symbol),
            raw_response=raw,
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
        previous_oldest: int | None = None
        while offset <= 1_000_000:
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
            if len(page) != len(raw):
                raise ValueError("WhiteBIT funding history contains malformed records")
            if page:
                oldest = min(int(item["fundingTime"]) for item in page)
                newest = max(int(item["fundingTime"]) for item in page)
                if previous_oldest is not None and newest >= previous_oldest:
                    raise RuntimeError("WhiteBIT funding pagination did not advance")
                previous_oldest = oldest
            rows.extend(page)
            if len(raw) < _FUNDING_LIMIT:
                break
            offset += len(raw)
        else:
            raise RuntimeError("WhiteBIT funding pagination exceeded the venue offset limit")
        timestamps = sorted(int(item["fundingTime"]) * 1000 for item in rows)
        if len(timestamps) < 2:
            raise ValueError(
                "WhiteBIT funding interval is unavailable from fewer than two settlements"
            )
        interval = timestamps[1] - timestamps[0]
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
    if not isinstance(result, list):
        raise ValueError("WhiteBIT kline result must be a list")
    return result
