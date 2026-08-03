"""ExchangeAdapter ABC and shared types for Koval exchange integrations.

Scope: read-only OHLCV access. Order placement, balances, and positions belong
to the sandbox broker adapters, not to this interface.
"""

from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from typing import Any, Final

import numpy as np
import requests

OHLCV_COLUMNS: Final[tuple[str, ...]] = (
    "timestamp_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

TIMEFRAMES: Final[tuple[str, ...]] = ("1m", "5m", "15m", "1h", "4h", "1d")

_TIMEFRAME_MS: Final[dict[str, int]] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_RETRYABLE_HTTP_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


def timeframe_ms(timeframe: str) -> int:
    """Return the duration of one candle in milliseconds."""
    try:
        return _TIMEFRAME_MS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unknown timeframe: {timeframe!r}") from exc


def normalize_ohlcv_range(
    candles: np.ndarray,
    *,
    start_ms: int,
    end_ms: int,
) -> np.ndarray:
    """Enforce the exchange-adapter OHLCV ordering and range contract."""
    try:
        rows = np.asarray(candles, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"OHLCV response must have shape (N, {len(OHLCV_COLUMNS)})") from exc
    if rows.size == 0:
        return np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
    if rows.ndim != 2 or rows.shape[1] != len(OHLCV_COLUMNS):
        raise ValueError(f"OHLCV response must have shape (N, {len(OHLCV_COLUMNS)})")
    if not np.isfinite(rows).all():
        raise ValueError("OHLCV response values must be finite")
    timestamps = rows[:, 0]
    if np.any(timestamps < 0) or np.any(timestamps != np.floor(timestamps)):
        raise ValueError("OHLCV timestamps must be non-negative integers")
    open_prices, highs, lows, closes, volumes = (rows[:, index] for index in range(1, 6))
    if np.any(np.column_stack((open_prices, highs, lows, closes)) <= 0):
        raise ValueError("OHLCV prices must be positive")
    if np.any(volumes < 0):
        raise ValueError("OHLCV volume must be non-negative")
    if np.any(highs < np.maximum.reduce((open_prices, lows, closes))) or np.any(
        lows > np.minimum.reduce((open_prices, highs, closes))
    ):
        raise ValueError("OHLCV high/low bounds are invalid")
    order = np.argsort(rows[:, 0], kind="mergesort")
    rows = rows[order]
    _, unique_indexes = np.unique(rows[:, 0], return_index=True)
    rows = rows[np.sort(unique_indexes)]
    timestamps = rows[:, 0]
    return rows[(timestamps >= start_ms) & (timestamps < end_ms)].copy()


def validate_transport_settings(
    *,
    timeout: float,
    page_limit: int,
    max_retries: int,
    retry_backoff_seconds: float,
) -> tuple[float, int, int, float]:
    """Validate and normalize read-only adapter transport controls."""
    if isinstance(timeout, bool) or isinstance(retry_backoff_seconds, bool):
        raise ValueError("invalid adapter transport settings")
    try:
        parsed_timeout = float(timeout)
        parsed_backoff = float(retry_backoff_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid adapter transport settings") from exc
    if (
        not math.isfinite(parsed_timeout)
        or parsed_timeout <= 0
        or not math.isfinite(parsed_backoff)
        or parsed_backoff < 0
        or isinstance(page_limit, bool)
        or not isinstance(page_limit, int)
        or page_limit <= 0
        or isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
    ):
        raise ValueError("invalid adapter transport settings")
    return parsed_timeout, page_limit, max_retries, parsed_backoff


def get_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, object],
    timeout: float,
    max_retries: int,
    backoff_seconds: float,
) -> requests.Response:
    """Issue a read-only request with bounded retries for transient failures."""
    for attempt in range(max_retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            if attempt == max_retries:
                raise
            delay = backoff_seconds * 2**attempt
        else:
            if response.status_code not in _RETRYABLE_HTTP_STATUSES or attempt == max_retries:
                return response
            retry_after = response.headers.get("Retry-After")
            try:
                delay = (
                    float(retry_after) if retry_after is not None else backoff_seconds * 2**attempt
                )
            except ValueError:
                delay = backoff_seconds * 2**attempt
        if delay > 0:
            time.sleep(min(delay, 60.0))
    raise AssertionError("retry loop must return a response")


class ExchangeAdapter(ABC):
    """Minimal data-plane contract every exchange integration implements.

    Subclasses fetch OHLCV candles via REST. The adapter is responsible for
    pagination — callers pass arbitrary [start_ms, end_ms) ranges and trust the
    adapter to issue as many HTTP calls as needed. The adapter returns the
    exchange's candles for the range as-is; ``OhlcvCache`` owns closed-only and
    forming-bar enforcement.
    """

    @abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> np.ndarray:
        """Return OHLCV candles whose open is in ``[start_ms, end_ms)``.

        Shape: ``(N, 6)``, dtype ``float64``, sorted ascending by ``timestamp_ms``,
        no duplicate timestamps. The result may include the currently-forming
        candle when the range reaches the present — ``OhlcvCache`` is responsible
        for dropping it. Returns an empty ``(0, 6)`` array if the exchange has no
        data in the range.
        """

    @abstractmethod
    def metadata(self) -> dict[str, Any]:
        """Return ``{"name": str, "symbols": list[str], "timeframes": list[str]}``.

        ``symbols`` is a curated list of pairs the adapter knows it can serve.
        Hosts may use it to populate symbol-selection interfaces.
        """
