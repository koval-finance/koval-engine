import numpy as np
import pytest

from koval.exchanges.base import (
    OHLCV_COLUMNS,
    TIMEFRAMES,
    ExchangeAdapter,
    normalize_ohlcv_range,
    timeframe_ms,
)


def test_ohlcv_columns_locked():
    assert OHLCV_COLUMNS == (
        "timestamp_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
    )


def test_timeframes_minimum_set():
    for tf in ("1m", "5m", "15m", "1h", "4h", "1d"):
        assert tf in TIMEFRAMES


def test_timeframe_ms_known_values():
    assert timeframe_ms("1m") == 60_000
    assert timeframe_ms("1h") == 3_600_000
    assert timeframe_ms("1d") == 86_400_000


def test_timeframe_ms_unknown_raises():
    with pytest.raises(ValueError, match="unknown timeframe"):
        timeframe_ms("2h")


def test_adapter_is_abstract():
    with pytest.raises(TypeError):
        ExchangeAdapter()  # type: ignore[abstract]


def test_fake_adapter_returns_ndarray(fake_adapter_factory):
    adapter = fake_adapter_factory(
        candles=[
            (1_700_000_000_000, 1.0, 2.0, 0.5, 1.5, 100.0),
            (1_700_000_060_000, 1.5, 2.5, 1.0, 2.0, 110.0),
        ],
    )
    out = adapter.fetch_ohlcv("BTC/USDT", "1m", 1_700_000_000_000, 1_700_000_120_000)
    assert isinstance(out, np.ndarray)
    assert out.shape == (2, 6)
    assert out.dtype == np.float64
    assert out[0, 0] == 1_700_000_000_000


@pytest.mark.parametrize(
    "row",
    [
        [0, 1, 2, 0.5, float("nan"), 1],
        [0.5, 1, 2, 0.5, 1.5, 1],
        [0, 0, 2, 0.5, 1.5, 1],
        [0, 1, 1.4, 0.5, 1.5, 1],
        [0, 1, 2, 1.1, 1.5, 1],
        [0, 1, 2, 0.5, 1.5, -1],
    ],
)
def test_normalize_ohlcv_range_rejects_malformed_market_data(row):
    with pytest.raises(ValueError, match="OHLCV"):
        normalize_ohlcv_range(
            np.asarray([row], dtype=np.float64),
            start_ms=0,
            end_ms=60_000,
        )
