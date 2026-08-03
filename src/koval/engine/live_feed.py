"""Live price feeds for paper trading (MIT, no Backtrader).

A ``LiveFeed`` yields confirmed-on-close OHLCV bars until a ``StopSignal`` is
set. ``ReplayFeed`` streams historical bars at an accelerated clock;
``PollingFeed`` polls the REST exchange adapter for the latest closed candle.
Each bar is a ``np.ndarray`` of shape ``(6,)``:
``[timestamp_ms, open, high, low, close, volume]``.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterator
from typing import Protocol

import numpy as np

from koval.exchanges.base import OHLCV_COLUMNS, timeframe_ms


class StopSignal:
    """An interruptible stop flag shared between the API and the session loop."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        """Block up to ``seconds``; return True if stop was signalled meanwhile."""
        return self._event.wait(seconds)


class LiveFeed(Protocol):
    """Yields confirmed-on-close bars until ``stop`` is set."""

    def bars(self, stop: StopSignal) -> Iterator[np.ndarray]: ...


def _validate_candles(candles: object) -> np.ndarray:
    message = f"replay feed candles must have shape (N, {len(OHLCV_COLUMNS)})"
    try:
        array = np.asarray(candles, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if array.ndim != 2 or array.shape[1] != len(OHLCV_COLUMNS):
        raise ValueError(message)
    return array


def _non_negative_seconds(value: object, *, subject: str, field: str) -> float:
    message = f"{subject} {field} must be a non-negative finite number"
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(message)
    return seconds


class ReplayFeed:
    """Streams a fixed candle array at an accelerated clock.

    ``delay_seconds`` is the (interruptible) pause between bars; tests use 0.0.
    """

    def __init__(self, candles: np.ndarray, *, delay_seconds: float = 0.0) -> None:
        self._candles = _validate_candles(candles)
        self._delay = _non_negative_seconds(
            delay_seconds, subject="replay feed", field="delay_seconds"
        )

    def bars(self, stop: StopSignal) -> Iterator[np.ndarray]:
        for row in self._candles:
            if stop.is_set():
                return
            yield row
            if self._delay > 0.0 and stop.wait(self._delay):
                return


_DEFAULT_LOOKBACK_BARS = 5


class FeedContinuityError(RuntimeError):
    """Raised when a polling feed cannot supply the next expected closed bar."""


def _now_ms() -> int:
    return int(time.time() * 1000)


class PollingFeed:
    """Polls a REST ``ExchangeAdapter`` for the latest CLOSED candle each interval.

    A candle is closed once ``ts + timeframe_ms <= now``; the still-forming candle
    is never emitted. Only bars newer than the last emitted timestamp are yielded.
    Adapter errors use bounded exponential backoff. A closed-bar continuity gap
    is reported through ``on_degraded`` and raises ``FeedContinuityError`` so a
    trading session cannot continue on incomplete market data.
    """

    def __init__(
        self,
        adapter,
        *,
        symbol: str,
        timeframe: str,
        clock: Callable[[], int] = _now_ms,
        poll_seconds: float | None = None,
        lookback_bars: int = _DEFAULT_LOOKBACK_BARS,
        start_after_ms: int | None = None,
        on_degraded: Callable[[str], None] | None = None,
    ) -> None:
        if not callable(getattr(adapter, "fetch_ohlcv", None)):
            raise ValueError(
                "polling feed adapter must expose a callable read-only "
                "fetch_ohlcv(symbol, timeframe, start_ms, end_ms)"
            )
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("polling feed symbol must be a non-empty string")
        tf_ms = timeframe_ms(timeframe)
        if not callable(clock):
            raise ValueError("polling feed clock must be callable")
        if on_degraded is not None and not callable(on_degraded):
            raise ValueError("polling feed on_degraded must be callable")
        if isinstance(lookback_bars, bool) or not isinstance(lookback_bars, int):
            raise ValueError("polling feed lookback_bars must be a positive integer")
        if lookback_bars <= 0:
            raise ValueError("polling feed lookback_bars must be a positive integer")
        if start_after_ms is not None:
            if isinstance(start_after_ms, bool) or not isinstance(start_after_ms, int):
                raise ValueError(
                    "polling feed start_after_ms must be a non-negative integer "
                    "aligned to the timeframe boundary"
                )
            if start_after_ms < 0 or start_after_ms % tf_ms != 0:
                raise ValueError(
                    "polling feed start_after_ms must be a non-negative integer "
                    "aligned to the timeframe boundary"
                )
        self._adapter = adapter
        self._symbol = symbol
        self._timeframe = timeframe
        self._clock = clock
        self._tf_ms = tf_ms
        self._poll = (
            tf_ms / 1000
            if poll_seconds is None
            else _non_negative_seconds(poll_seconds, subject="polling feed", field="poll_seconds")
        )
        self._lookback = lookback_bars
        self._last_ts = start_after_ms
        self._on_degraded = on_degraded or (lambda _msg: None)

    def bars(self, stop: StopSignal) -> Iterator[np.ndarray]:
        backoff = 1.0
        while not stop.is_set():
            # An unusable clock is a configuration fault, not a venue outage, so
            # it must never be absorbed by the transient-error backoff.
            now = self._now()
            try:
                start = now - self._tf_ms * (self._lookback + 1)
                candles = self._adapter.fetch_ohlcv(self._symbol, self._timeframe, start, now)
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - a transient outage must not kill the session
                self._on_degraded(str(exc))
                if stop.wait(min(backoff, 60.0)):
                    return
                backoff *= 2
                continue
            for row in np.asarray(candles, dtype=float):
                ts = int(row[0])
                closed = ts + self._tf_ms <= now
                fresh = self._last_ts is None or ts > self._last_ts
                if closed and fresh:
                    if self._last_ts is not None:
                        expected = self._last_ts + self._tf_ms
                        if ts != expected:
                            message = (
                                f"OHLCV continuity gap for {self._symbol} "
                                f"{self._timeframe}: expected {expected}, received {ts}"
                            )
                            self._on_degraded(message)
                            raise FeedContinuityError(message)
                    self._last_ts = ts
                    yield row
            if stop.wait(self._poll):
                return

    def _now(self) -> int:
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError(
                "polling feed clock must return a non-negative integer millisecond timestamp"
            )
        return now


__all__ = [
    "FeedContinuityError",
    "StopSignal",
    "LiveFeed",
    "ReplayFeed",
    "PollingFeed",
    "OHLCV_COLUMNS",
]
