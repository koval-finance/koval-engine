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
_DEFAULT_MAX_CONSECUTIVE_FAILURES = 5


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
    trading session cannot continue on incomplete market data. ``between_bars``
    is invoked every ``between_bars_seconds`` while waiting for the next closed
    bar, so a caller can watch working orders between decisions.
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
        between_bars: Callable[[], None] | None = None,
        between_bars_seconds: float = 5.0,
        max_consecutive_failures: int = _DEFAULT_MAX_CONSECUTIVE_FAILURES,
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
        if between_bars is not None and not callable(between_bars):
            raise ValueError("polling feed between_bars must be callable")
        if isinstance(lookback_bars, bool) or not isinstance(lookback_bars, int):
            raise ValueError("polling feed lookback_bars must be a positive integer")
        if lookback_bars <= 0:
            raise ValueError("polling feed lookback_bars must be a positive integer")
        if isinstance(max_consecutive_failures, bool) or not isinstance(
            max_consecutive_failures, int
        ):
            raise ValueError("polling feed max_consecutive_failures must be a positive integer")
        if max_consecutive_failures <= 0:
            raise ValueError("polling feed max_consecutive_failures must be a positive integer")
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
        self._between_bars = between_bars
        self._between_bars_seconds = _non_negative_seconds(
            between_bars_seconds, subject="polling feed", field="between_bars_seconds"
        )
        self._max_consecutive_failures = max_consecutive_failures

    def bars(self, stop: StopSignal) -> Iterator[np.ndarray]:
        backoff = 1.0
        consecutive_failures = 0
        while not stop.is_set():
            # An unusable clock is a configuration fault, not a venue outage, so
            # it must never be absorbed by the transient-error backoff.
            now = self._now()
            try:
                start = now - self._tf_ms * (self._lookback + 1)
                candles = self._fetch_with_supervision(stop, start=start, end=now)
                if candles is None:
                    return
                backoff = 1.0
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001 - a transient outage must not kill the session
                consecutive_failures += 1
                self._on_degraded(str(exc))
                if consecutive_failures >= self._max_consecutive_failures:
                    message = (
                        "market_data_retry_exhausted: "
                        f"{self._symbol} {self._timeframe} after "
                        f"{consecutive_failures} consecutive failures"
                    )
                    self._on_degraded(message)
                    raise FeedContinuityError(message) from exc
                if self._wait_with_supervision(stop, min(backoff, 60.0)):
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
            if self._wait_for_next_poll(stop):
                return

    def _wait_for_next_poll(self, stop: StopSignal) -> bool:
        """Wait one poll interval, running the between-bars hook meanwhile.

        A limit or stop entry can fill seconds after it is placed; the hook lets
        the caller protect that fill without waiting for the next bar close.
        Returns True when the session was stopped.
        """
        return self._wait_with_supervision(stop, self._poll)

    def _fetch_with_supervision(self, stop: StopSignal, *, start: int, end: int) -> object | None:
        if self._between_bars is None:
            return self._adapter.fetch_ohlcv(self._symbol, self._timeframe, start, end)
        done = threading.Event()
        outcome: list[tuple[str, object]] = []

        def fetch() -> None:
            try:
                value = self._adapter.fetch_ohlcv(self._symbol, self._timeframe, start, end)
            except BaseException as exc:  # re-raised on the feed thread
                outcome.append(("error", exc))
            else:
                outcome.append(("value", value))
            finally:
                done.set()

        threading.Thread(target=fetch, name="koval-market-data-fetch", daemon=True).start()
        step = max(self._between_bars_seconds, 0.001)
        while not done.wait(step):
            self._between_bars()
            if stop.is_set():
                return None
        kind, value = outcome[0]
        if kind == "error":
            raise value  # type: ignore[misc]
        return value

    def _wait_with_supervision(self, stop: StopSignal, duration: float) -> bool:
        if self._between_bars is None:
            return stop.wait(duration)
        remaining = duration
        step = max(self._between_bars_seconds, 0.001)
        while remaining > 0:
            self._between_bars()
            waited = min(step, remaining)
            if stop.wait(waited):
                return True
            remaining -= waited
        return False

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
