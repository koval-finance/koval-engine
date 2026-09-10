from itertools import islice

import numpy as np
import pytest

from koval.engine.live_feed import FeedContinuityError, ReplayFeed, StopSignal


def _candles(n: int) -> np.ndarray:
    # [ts_ms, open, high, low, close, volume]
    rows = []
    for i in range(n):
        ts = i * 60_000
        rows.append([ts, 100 + i, 101 + i, 99 + i, 100.5 + i, 10.0])
    return np.array(rows, dtype=float)


def test_stop_signal_wait_returns_true_when_set():
    stop = StopSignal()
    assert stop.is_set() is False
    stop.set()
    assert stop.is_set() is True
    assert stop.wait(0.0) is True


def test_replay_feed_yields_all_bars_in_order():
    feed = ReplayFeed(_candles(3), delay_seconds=0.0)
    out = list(feed.bars(StopSignal()))
    assert len(out) == 3
    assert [int(r[0]) for r in out] == [0, 60_000, 120_000]


def test_replay_feed_stops_when_signalled():
    stop = StopSignal()
    stop.set()
    feed = ReplayFeed(_candles(5), delay_seconds=0.0)
    out = list(feed.bars(stop))
    assert out == []


@pytest.mark.parametrize(
    "candles",
    [
        np.ones(6),
        np.ones((2, 5)),
        np.ones((2, 7)),
    ],
)
def test_replay_feed_rejects_malformed_candle_arrays(candles):
    with pytest.raises(ValueError, match="shape"):
        ReplayFeed(candles)


@pytest.mark.parametrize("delay", [-1, float("nan"), float("inf"), True, "invalid"])
def test_replay_feed_rejects_invalid_delay(delay):
    with pytest.raises(ValueError, match="delay"):
        ReplayFeed(_candles(1), delay_seconds=delay)


class _FakeAdapter:
    """Returns a fixed candle block; the last row is the still-forming candle."""

    def __init__(self, candles: np.ndarray) -> None:
        self._candles = candles
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
        self.calls += 1
        return self._candles


def test_polling_feed_emits_only_closed_new_bars():
    from koval.engine.live_feed import PollingFeed

    # three closed bars (0, 60k, 120k) + one forming bar (180k)
    candles = _candles(4)
    # "now" is during the 180k bar -> bar 180k is still forming and must be dropped
    now_ms = 180_000 + 30_000
    adapter = _FakeAdapter(candles)
    feed = PollingFeed(
        adapter,
        symbol="BTCUSDT",
        timeframe="1m",
        clock=lambda: now_ms,
        poll_seconds=0.0,
    )
    out = list(islice(feed.bars(StopSignal()), 3))
    assert [int(r[0]) for r in out] == [0, 60_000, 120_000]


def test_polling_feed_does_not_re_emit_seen_bars():
    from koval.engine.live_feed import PollingFeed

    candles = _candles(4)
    now_ms = 240_000  # all four bars now closed
    feed = PollingFeed(
        _FakeAdapter(candles),
        symbol="BTCUSDT",
        timeframe="1m",
        clock=lambda: now_ms,
        poll_seconds=0.0,
    )
    gen = feed.bars(StopSignal())
    first = list(islice(gen, 4))
    assert [int(r[0]) for r in first] == [0, 60_000, 120_000, 180_000]


def test_polling_feed_reports_and_stops_on_continuity_gap():
    from koval.engine.live_feed import PollingFeed

    degraded = []
    candles = _candles(6)[3:]  # first returned bar is 180k; expected next is 120k
    feed = PollingFeed(
        _FakeAdapter(candles),
        symbol="BTCUSDT",
        timeframe="1m",
        clock=lambda: 360_000,
        poll_seconds=0.0,
        start_after_ms=60_000,
        on_degraded=degraded.append,
    )

    with pytest.raises(FeedContinuityError, match="expected 120000, received 180000"):
        next(feed.bars(StopSignal()))

    assert degraded == ["OHLCV continuity gap for BTCUSDT 1m: expected 120000, received 180000"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": ""},
        {"poll_seconds": -1},
        {"poll_seconds": float("nan")},
        {"poll_seconds": float("inf")},
        {"poll_seconds": True},
        {"lookback_bars": 0},
        {"lookback_bars": 1.5},
        {"lookback_bars": True},
        {"max_consecutive_failures": 0},
        {"max_consecutive_failures": True},
        {"start_after_ms": -1},
        {"start_after_ms": 1},
        {"start_after_ms": True},
        {"clock": None},
        {"on_degraded": "not-callable"},
    ],
)
def test_polling_feed_rejects_invalid_configuration(overrides):
    from koval.engine.live_feed import PollingFeed

    values = {
        "adapter": _FakeAdapter(_candles(1)),
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "clock": lambda: 60_000,
        "poll_seconds": 0.0,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match="polling feed"):
        PollingFeed(**values)


def test_polling_feed_rejects_an_adapter_without_the_read_only_contract():
    from koval.engine.live_feed import PollingFeed

    with pytest.raises(ValueError, match="adapter"):
        PollingFeed(object(), symbol="BTCUSDT", timeframe="1m")


@pytest.mark.parametrize("now", [-1, float("nan"), float("inf"), True, 1.5])
def test_polling_feed_rejects_invalid_clock_values_without_retrying(now):
    from koval.engine.live_feed import PollingFeed

    class NoRetryStop(StopSignal):
        def wait(self, seconds):
            raise AssertionError(f"invalid clock was retried after {seconds} seconds")

    adapter = _FakeAdapter(_candles(1))
    feed = PollingFeed(
        adapter,
        symbol="BTCUSDT",
        timeframe="1m",
        clock=lambda: now,
        poll_seconds=0.0,
    )

    with pytest.raises(ValueError, match="clock"):
        next(feed.bars(NoRetryStop()))

    assert adapter.calls == 0


def test_polling_feed_calls_between_bars_hook_while_waiting():
    import threading

    from koval.engine.live_feed import PollingFeed

    calls = []
    feed = PollingFeed(
        _FakeAdapter(_candles(2)),
        symbol="BTCUSDT",
        timeframe="1m",
        clock=lambda: 120_000,
        poll_seconds=1.0,
        between_bars=lambda: calls.append(1),
        between_bars_seconds=0.005,
    )
    stop = StopSignal()
    bars = feed.bars(stop)
    next(bars)
    next(bars)
    threading.Timer(0.06, stop.set).start()
    list(bars)
    assert len(calls) >= 3


def test_polling_feed_rejects_a_non_callable_between_bars_hook():
    from koval.engine.live_feed import PollingFeed

    with pytest.raises(ValueError, match="between_bars"):
        PollingFeed(
            _FakeAdapter(_candles(2)),
            symbol="BTCUSDT",
            timeframe="1m",
            between_bars="not-callable",
        )


def test_polling_feed_keeps_order_supervision_alive_during_data_outage():
    from koval.engine.live_feed import PollingFeed

    class FlakyAdapter:
        def __init__(self):
            self.calls = 0

        def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("public data unavailable")
            return _candles(1)

    class ImmediateWaitStop(StopSignal):
        def wait(self, seconds):
            return False

    supervised = []
    feed = PollingFeed(
        FlakyAdapter(),
        symbol="BTCUSDT",
        timeframe="1m",
        clock=lambda: 60_000,
        poll_seconds=0.0,
        between_bars=lambda: supervised.append("polled"),
        between_bars_seconds=0.25,
    )

    bar = next(feed.bars(ImmediateWaitStop()))

    assert int(bar[0]) == 0
    assert len(supervised) == 4


def test_polling_feed_supervises_orders_while_http_fetch_is_blocked():
    import time

    from koval.engine.live_feed import PollingFeed

    class SlowAdapter:
        def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
            time.sleep(0.04)
            return _candles(1)

    supervised = []
    feed = PollingFeed(
        SlowAdapter(),
        symbol="BTCUSDT",
        timeframe="1m",
        clock=lambda: 60_000,
        poll_seconds=0.0,
        between_bars=lambda: supervised.append("polled"),
        between_bars_seconds=0.005,
    )

    bar = next(feed.bars(StopSignal()))

    assert int(bar[0]) == 0
    assert len(supervised) >= 3


def test_polling_feed_fails_with_stable_reason_after_bounded_outage_retries():
    from koval.engine.live_feed import PollingFeed

    class OfflineAdapter:
        def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
            raise RuntimeError("offline")

    class ImmediateWaitStop(StopSignal):
        def wait(self, seconds):
            return False

    feed = PollingFeed(
        OfflineAdapter(),
        symbol="BTCUSDT",
        timeframe="1m",
        clock=lambda: 60_000,
        poll_seconds=0.0,
        max_consecutive_failures=2,
    )

    with pytest.raises(FeedContinuityError, match="market_data_retry_exhausted"):
        next(feed.bars(ImmediateWaitStop()))
