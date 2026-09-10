"""Causal aggregation of confirmed higher-timeframe OHLCV candles."""

from __future__ import annotations

import numpy as np

from koval.exchanges.base import OHLCV_COLUMNS, timeframe_ms


def confirmed_higher_timeframe_bars(
    candles: np.ndarray,
    *,
    source_timeframe: str,
    target_timeframe: str,
    decision_time_ms: int,
) -> np.ndarray:
    source_ms = timeframe_ms(source_timeframe)
    target_ms = timeframe_ms(target_timeframe)
    if target_ms <= source_ms or target_ms % source_ms:
        raise ValueError("target timeframe must be a larger integer multiple of source timeframe")
    values = np.asarray(candles, dtype=np.float64)
    if values.size == 0:
        return np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != len(OHLCV_COLUMNS):
        raise ValueError("higher-timeframe source candles must have shape (N, 6)")

    complete_rows: list[list[float]] = []
    expected_count = target_ms // source_ms
    timestamps = values[:, 0].astype(np.int64)
    window_start = int(timestamps.min())
    bucket_starts = np.unique((timestamps // target_ms) * target_ms)
    for bucket_start in bucket_starts:
        bucket_end = int(bucket_start) + target_ms
        if bucket_end > int(decision_time_ms):
            continue
        # A rolling live window opens wherever the feed did and slides bar by
        # bar, so its first bucket is usually truncated. That is a coverage
        # boundary, not a hole: only buckets the source fully spans are judged.
        if int(bucket_start) < window_start:
            continue
        bucket = values[(values[:, 0] >= bucket_start) & (values[:, 0] < bucket_end)]
        expected_timestamps = np.arange(bucket_start, bucket_end, source_ms, dtype=np.int64)
        actual_timestamps = bucket[:, 0].astype(np.int64)
        if len(bucket) != expected_count or not np.array_equal(
            actual_timestamps, expected_timestamps
        ):
            raise ValueError(
                f"incomplete higher-timeframe source coverage for bucket {int(bucket_start)}"
            )
        complete_rows.append(
            [
                float(bucket_start),
                float(bucket[0, 1]),
                float(np.max(bucket[:, 2])),
                float(np.min(bucket[:, 3])),
                float(bucket[-1, 4]),
                float(np.sum(bucket[:, 5])),
            ]
        )
    if not complete_rows:
        return np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
    return np.asarray(complete_rows, dtype=np.float64)


__all__ = ["confirmed_higher_timeframe_bars"]
