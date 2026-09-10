import numpy as np
import pytest

from koval.engine.market_data import (
    build_candle_dataset,
    canonical_candle_bytes,
    decode_canonical_candles,
)


def _candles():
    return np.array(
        [
            [0, 100, 101, 99, 100.5, 0],
            [60_000, 100.5, 102, 100, 101, 12.5],
        ],
        dtype=np.float64,
    )


def test_canonical_candle_encoding_is_deterministic_and_round_trips_exactly():
    first = canonical_candle_bytes(_candles())
    second = canonical_candle_bytes(_candles().copy())

    assert first == second
    assert first.startswith(b"KOVAL-CANDLES-V1\n")
    assert np.array_equal(decode_canonical_candles(first), _candles())


def test_dataset_exposes_identity_coverage_quality_and_observed_zero():
    dataset = build_candle_dataset(
        _candles(),
        exchange="binance",
        market="future",
        symbol="btc/usdt",
        timeframe="1m",
        requested_start_ms=0,
        requested_end_ms=120_000,
        source="binance_rest_klines",
    )

    assert dataset.metadata.canonical_symbol == "BTCUSDT"
    assert dataset.metadata.actual_start_ms == 0
    assert dataset.metadata.actual_end_ms == 120_000
    assert dataset.metadata.gaps == ()
    assert dataset.metadata.duplicate_timestamps == ()
    assert dataset.metadata.volume_status == "observed"
    assert dataset.metadata.encoding_version == "koval_candles_f64le_v1"
    assert len(dataset.sha256) == 64
    with pytest.raises(ValueError):
        dataset.candles.setflags(write=True)


def test_dataset_rejects_duplicate_or_incomplete_coverage():
    duplicate = np.vstack([_candles(), _candles()[1]])
    with pytest.raises(ValueError, match="duplicate candle timestamp"):
        build_candle_dataset(
            duplicate,
            exchange="binance",
            market="future",
            symbol="BTCUSDT",
            timeframe="1m",
            requested_start_ms=0,
            requested_end_ms=120_000,
            source="test",
        )

    with pytest.raises(ValueError, match="incomplete candle coverage"):
        build_candle_dataset(
            _candles()[1:],
            exchange="binance",
            market="future",
            symbol="BTCUSDT",
            timeframe="1m",
            requested_start_ms=0,
            requested_end_ms=120_000,
            source="test",
        )


@pytest.mark.parametrize(
    "row",
    [
        [0, 100, 99, 98, 100, 1],
        [0, 100, 101, 99, 100, -1],
        [0, 0, 1, 0, 0.5, 1],
    ],
)
def test_canonical_evidence_rejects_invalid_ohlcv_values(row):
    with pytest.raises(ValueError, match="canonical candle"):
        build_candle_dataset(
            np.array([row], dtype=float),
            exchange="binance",
            market="future",
            symbol="BTCUSDT",
            timeframe="1m",
            requested_start_ms=0,
            requested_end_ms=60_000,
            source="test",
        )
