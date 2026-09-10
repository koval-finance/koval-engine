from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from koval.exchanges.base import OHLCV_COLUMNS
from koval.exchanges.ohlcv_cache import OhlcvCache, OhlcvContinuityError


@pytest.fixture
def cache(tmp_path):
    return OhlcvCache(root=tmp_path, now_ms=lambda: 1_800_000_000_000)


def _candles(start_ms: int, n: int, step: int = 60_000) -> list[tuple[float, ...]]:
    out = []
    for i in range(n):
        ts = start_ms + i * step
        out.append((float(ts), 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 100.0 + i))
    return out


def test_full_miss_fetches_from_adapter_and_persists(cache, fake_adapter_factory):
    adapter = fake_adapter_factory(_candles(1_700_000_000_000, 10))
    out = cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_600_000,
    )
    assert out.shape == (10, len(OHLCV_COLUMNS))
    assert out[0, 0] == 1_700_000_000_000
    assert out[-1, 0] == 1_700_000_540_000
    assert len(adapter.calls) == 1


def test_evidence_get_returns_archive_ready_identity(cache, fake_adapter_factory):
    start = 1_700_000_000_000
    adapter = fake_adapter_factory(_candles(start, 2), exchange="binance", market="future")

    dataset = cache.get_evidence(
        adapter,
        exchange="binance",
        exchange_type="future",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=start,
        end_ms=start + 120_000,
    )

    assert dataset.metadata.exchange == "binance"
    assert dataset.metadata.market == "future"
    assert dataset.metadata.source == "binance:future:production:ohlcv"
    assert dataset.metadata.row_count == 2
    assert dataset.sha256


def test_evidence_get_rejects_a_range_that_contains_the_forming_bar(tmp_path, fake_adapter_factory):
    cache = OhlcvCache(root=tmp_path, now_ms=lambda: 120_000)
    adapter = fake_adapter_factory(_candles(0, 3), exchange="binance", market="future")

    with pytest.raises(ValueError, match="closed bars"):
        cache.get_evidence(
            adapter,
            exchange="binance",
            exchange_type="future",
            symbol="BTC/USDT",
            timeframe="1m",
            start_ms=0,
            end_ms=180_000,
        )

    assert adapter.calls == []


def test_full_hit_does_not_call_adapter(cache, fake_adapter_factory):
    adapter = fake_adapter_factory(_candles(1_700_000_000_000, 10))
    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_600_000,
    )
    adapter.calls.clear()

    out = cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_600_000,
    )
    assert out.shape == (10, len(OHLCV_COLUMNS))
    assert adapter.calls == []


def test_parquet_file_layout(cache, fake_adapter_factory, tmp_path):
    adapter = fake_adapter_factory(
        _candles(1_700_000_000_000, 5), exchange="binance", market="future"
    )
    cache.get(
        adapter,
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="1h",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_300_000,
    )
    expected = tmp_path / "binance" / "future" / "BTCUSDT_1h.parquet"
    assert expected.is_file()


def test_empty_range_returns_empty(cache, fake_adapter_factory):
    adapter = fake_adapter_factory([])
    out = cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_060_000,
    )
    assert out.shape == (0, len(OHLCV_COLUMNS))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exchange", "../../outside"),
        ("symbol", "../BTC"),
        ("timeframe", "../../1m"),
    ],
)
def test_cache_rejects_path_traversal_before_creating_directories(
    tmp_path, fake_adapter_factory, field, value
):
    cache = OhlcvCache(root=tmp_path / "cache", now_ms=lambda: 1_800_000_000_000)
    arguments = {
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "timeframe": "1m",
    }
    arguments[field] = value

    with pytest.raises(ValueError):
        cache.get(
            fake_adapter_factory([]),
            **arguments,
            start_ms=1_700_000_000_000,
            end_ms=1_700_000_060_000,
        )

    assert not (tmp_path / "outside").exists()


def test_tail_gap_fetches_only_tail(cache, fake_adapter_factory):
    adapter = fake_adapter_factory(_candles(1_700_000_000_000, 20))
    # Seed: first 10 candles cached.
    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_600_000,
    )
    adapter.calls.clear()

    # Request first 20 — only [600_000, 1_200_000) must be fetched.
    out = cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_001_200_000,
    )
    assert out.shape == (20, len(OHLCV_COLUMNS))
    assert len(adapter.calls) == 1
    _, _, gap_start, gap_end = adapter.calls[0]
    assert gap_start == 1_700_000_600_000
    assert gap_end == 1_700_001_200_000


def test_head_gap_fetches_only_head(cache, fake_adapter_factory):
    adapter = fake_adapter_factory(_candles(1_700_000_000_000, 20))
    # Seed: candles 10..20 cached.
    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_600_000,
        end_ms=1_700_001_200_000,
    )
    adapter.calls.clear()

    out = cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_001_200_000,
    )
    assert out.shape == (20, len(OHLCV_COLUMNS))
    assert len(adapter.calls) == 1
    _, _, gap_start, gap_end = adapter.calls[0]
    assert gap_start == 1_700_000_000_000
    assert gap_end == 1_700_000_600_000


def test_both_ends_gap_fetches_two_segments(cache, fake_adapter_factory):
    adapter = fake_adapter_factory(_candles(1_700_000_000_000, 30))
    # Seed middle: candles 10..20 cached.
    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_600_000,
        end_ms=1_700_001_200_000,
    )
    adapter.calls.clear()

    out = cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_001_800_000,
    )
    assert out.shape == (30, len(OHLCV_COLUMNS))
    assert len(adapter.calls) == 2
    ranges = sorted((s, e) for (_, _, s, e) in adapter.calls)
    assert ranges[0] == (1_700_000_000_000, 1_700_000_600_000)
    assert ranges[1] == (1_700_001_200_000, 1_700_001_800_000)


def test_persisted_file_is_sorted_and_unique(cache, fake_adapter_factory, tmp_path):
    adapter = fake_adapter_factory(_candles(1_700_000_000_000, 20))
    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_600_000,
        end_ms=1_700_001_200_000,
    )
    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_001_200_000,
    )

    df = pd.read_parquet(tmp_path / "fake" / "future" / "BTCUSDT_1m.parquet")
    ts = df["timestamp_ms"].to_numpy()
    assert (np.diff(ts) > 0).all()
    assert len(ts) == len(set(ts))


def test_sort_dedupe_keeps_first_occurrence_on_overlap():
    duplicated = np.array(
        [
            [3.0, 3, 4, 2.5, 3.5, 120],
            [1.0, 1, 2, 0.5, 1.5, 100],
            [2.0, 2, 3, 1.5, 2.5, 110],
            [1.0, 99, 99, 99, 99, 99],
            [2.0, 88, 88, 88, 88, 88],
        ],
        dtype=np.float64,
    )

    out = OhlcvCache._sort_dedupe(duplicated)

    assert out.shape == (3, 6)
    np.testing.assert_array_equal(out[:, 0], [1.0, 2.0, 3.0])
    assert out[0, 1] == 1
    assert out[1, 1] == 2
    assert out[2, 1] == 3


_ALIGNED_BASE = 1_700_006_400_000  # divisible by 1m, 1h, and 1d step
assert _ALIGNED_BASE % 86_400_000 == 0, "forming-bar tests rely on a 1d-aligned base"


def test_currently_forming_bar_always_refetched(tmp_path, fake_adapter_factory):
    forming_open = _ALIGNED_BASE + 600_000  # 11th candle slot
    cache = OhlcvCache(root=tmp_path, now_ms=lambda: _ALIGNED_BASE + 650_000)
    adapter = fake_adapter_factory(_candles(_ALIGNED_BASE, 11))

    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=_ALIGNED_BASE,
        end_ms=_ALIGNED_BASE + 700_000,
    )
    adapter.calls.clear()

    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=_ALIGNED_BASE,
        end_ms=_ALIGNED_BASE + 700_000,
    )

    assert len(adapter.calls) == 1
    _, _, refetch_start, _ = adapter.calls[0]
    assert refetch_start == forming_open


def test_forming_bar_not_persisted(tmp_path, fake_adapter_factory):
    cache = OhlcvCache(root=tmp_path, now_ms=lambda: _ALIGNED_BASE + 650_000)
    adapter = fake_adapter_factory(_candles(_ALIGNED_BASE, 11))

    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=_ALIGNED_BASE,
        end_ms=_ALIGNED_BASE + 700_000,
    )

    df = pd.read_parquet(tmp_path / "fake" / "future" / "BTCUSDT_1m.parquet")
    assert df["timestamp_ms"].max() == _ALIGNED_BASE + 540_000


def test_disjoint_requests_keep_parquet_contiguous(cache, fake_adapter_factory, tmp_path):
    adapter = fake_adapter_factory(_candles(1_700_000_000_000, 30))

    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_600_000,
    )
    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_001_200_000,
        end_ms=1_700_001_800_000,
    )

    df = pd.read_parquet(tmp_path / "fake" / "future" / "BTCUSDT_1m.parquet")
    ts = df["timestamp_ms"].to_numpy()
    expected = np.array([1_700_000_000_000 + i * 60_000 for i in range(30)], dtype=np.int64)
    np.testing.assert_array_equal(ts, expected)

    adapter.calls.clear()
    out = cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_720_000,
        end_ms=1_700_001_080_000,
    )
    assert out.shape == (6, len(OHLCV_COLUMNS))
    assert out[0, 0] == 1_700_000_720_000
    assert out[-1, 0] == 1_700_001_020_000
    assert adapter.calls == []


def test_internal_hole_is_refetched_and_repaired(cache, fake_adapter_factory, tmp_path):
    start_ms = 1_700_000_000_000
    adapter = fake_adapter_factory(_candles(start_ms, 10))
    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=start_ms,
        end_ms=start_ms + 600_000,
    )
    path = tmp_path / "fake" / "future" / "BTCUSDT_1m.parquet"
    damaged = pd.read_parquet(path).drop(index=4)
    damaged.to_parquet(path, index=False)
    adapter.calls.clear()

    out = cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=start_ms,
        end_ms=start_ms + 600_000,
    )

    assert out.shape == (10, len(OHLCV_COLUMNS))
    assert adapter.calls == [("BTC/USDT", "1m", start_ms + 240_000, start_ms + 300_000)]
    repaired = pd.read_parquet(path)
    np.testing.assert_array_equal(
        repaired["timestamp_ms"].to_numpy(),
        np.array([start_ms + i * 60_000 for i in range(10)], dtype=np.int64),
    )


def test_unresolved_internal_hole_fails_without_rewriting_cache(
    cache, fake_adapter_factory, tmp_path
):
    start_ms = 1_700_000_000_000
    complete = fake_adapter_factory(_candles(start_ms, 10))
    cache.get(
        complete,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=start_ms,
        end_ms=start_ms + 600_000,
    )
    path = tmp_path / "fake" / "future" / "BTCUSDT_1m.parquet"
    damaged = pd.read_parquet(path).drop(index=4)
    damaged.to_parquet(path, index=False)
    original = path.read_bytes()
    unavailable = fake_adapter_factory([])

    with pytest.raises(OhlcvContinuityError, match="unresolved OHLCV continuity gap"):
        cache.get(
            unavailable,
            exchange="fake",
            symbol="BTC/USDT",
            timeframe="1m",
            start_ms=start_ms,
            end_ms=start_ms + 600_000,
        )

    assert path.read_bytes() == original


def test_failed_write_preserves_existing_parquet(
    cache, fake_adapter_factory, tmp_path, monkeypatch
):
    start_ms = 1_700_000_000_000
    adapter = fake_adapter_factory(_candles(start_ms, 3))
    cache.get(
        adapter,
        exchange="fake",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=start_ms,
        end_ms=start_ms + 180_000,
    )
    path = tmp_path / "fake" / "future" / "BTCUSDT_1m.parquet"
    original = path.read_bytes()

    def fail_after_partial_write(self, target, *, index):
        target.write_bytes(b"incomplete parquet")
        raise RuntimeError("simulated parquet write failure")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="simulated parquet write failure"):
        cache._write(path, np.asarray(_candles(start_ms, 4), dtype=np.float64))

    assert path.read_bytes() == original


def test_parallel_writes_keep_parquet_sorted_and_unique(tmp_path, fake_adapter_factory):
    import threading

    cache = OhlcvCache(root=tmp_path, now_ms=lambda: 1_800_000_000_000)
    adapter_a = fake_adapter_factory(_candles(1_700_000_000_000, 10))
    adapter_b = fake_adapter_factory(_candles(1_700_000_600_000, 10))

    def call(adapter, start, end):
        cache.get(
            adapter,
            exchange="fake",
            symbol="BTC/USDT",
            timeframe="1m",
            start_ms=start,
            end_ms=end,
        )

    t1 = threading.Thread(
        target=call,
        args=(adapter_a, 1_700_000_000_000, 1_700_000_600_000),
    )
    t2 = threading.Thread(
        target=call,
        args=(adapter_b, 1_700_000_600_000, 1_700_001_200_000),
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    df = pd.read_parquet(tmp_path / "fake" / "future" / "BTCUSDT_1m.parquet")
    ts = df["timestamp_ms"].to_numpy()
    assert (np.diff(ts) > 0).all()
    assert len(ts) == 20


def test_spot_and_futures_never_share_a_file(cache, fake_adapter_factory, tmp_path):
    futures = fake_adapter_factory(_candles(1_700_000_000_000, 10), market="future")
    spot = fake_adapter_factory(_candles(1_700_000_000_000, 10), market="spot")
    kwargs = dict(
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_600_000,
    )
    cache.get(futures, exchange="fake", exchange_type="future", **kwargs)
    cache.get(spot, exchange="fake", exchange_type="spot", **kwargs)
    assert (tmp_path / "fake" / "future" / "BTCUSDT_1m.parquet").is_file()
    assert (tmp_path / "fake" / "spot" / "BTCUSDT_1m.parquet").is_file()
    assert len(futures.calls) == 1 and len(spot.calls) == 1


def test_legacy_binance_file_is_adopted_as_futures(cache, fake_adapter_factory, tmp_path):
    legacy = tmp_path / "binance" / "BTCUSDT_1m.parquet"
    legacy.parent.mkdir(parents=True)
    pd.DataFrame(_candles(1_700_000_000_000, 10), columns=OHLCV_COLUMNS).to_parquet(
        legacy, index=False
    )
    adapter = fake_adapter_factory([], exchange="binance", market="future")
    out = cache.get(
        adapter,
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="1m",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_600_000,
    )
    assert out.shape[0] == 10 and adapter.calls == []
    assert not legacy.exists()
    assert (tmp_path / "binance" / "future" / "BTCUSDT_1m.parquet").is_file()


def test_adapter_identity_mismatch_fails(cache, fake_adapter_factory):
    adapter = fake_adapter_factory([], exchange="binance", market="future")
    with pytest.raises(ValueError, match="identity"):
        cache.get(
            adapter,
            exchange="whitebit",
            symbol="BTC/USDT",
            timeframe="1m",
            start_ms=0,
            end_ms=60_000,
        )
