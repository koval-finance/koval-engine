import numpy as np
import pytest

from koval.engine.higher_timeframe import confirmed_higher_timeframe_bars


def _minute_rows(count):
    return np.array(
        [[i * 60_000, 100 + i, 102 + i, 99 + i, 101 + i, 10 + i] for i in range(count)],
        dtype=float,
    )


def test_higher_timeframe_aggregation_excludes_the_still_open_bucket():
    bars = confirmed_higher_timeframe_bars(
        _minute_rows(7),
        source_timeframe="1m",
        target_timeframe="5m",
        decision_time_ms=7 * 60_000,
    )

    assert bars.shape == (1, 6)
    assert bars[0].tolist() == [0.0, 100.0, 106.0, 99.0, 105.0, 60.0]


def test_higher_timeframe_aggregation_requires_complete_source_coverage():
    incomplete = np.delete(_minute_rows(5), 2, axis=0)

    with pytest.raises(ValueError, match="incomplete higher-timeframe source coverage"):
        confirmed_higher_timeframe_bars(
            incomplete,
            source_timeframe="1m",
            target_timeframe="5m",
            decision_time_ms=5 * 60_000,
        )


def test_target_timeframe_must_be_a_larger_integer_multiple():
    with pytest.raises(ValueError, match="integer multiple"):
        confirmed_higher_timeframe_bars(
            _minute_rows(5),
            source_timeframe="1h",
            target_timeframe="5m",
            decision_time_ms=5 * 60_000,
        )


def test_window_beginning_mid_bucket_skips_that_bucket_instead_of_raising():
    """A live rolling window starts wherever the feed did and slides bar by bar,
    so its first higher-timeframe bucket is almost always truncated. That is a
    coverage boundary, not a hole in the data."""
    rows = _minute_rows(12)[2:]  # window opens two minutes into the first bucket

    bars = confirmed_higher_timeframe_bars(
        rows,
        source_timeframe="1m",
        target_timeframe="5m",
        decision_time_ms=12 * 60_000,
    )

    assert bars[:, 0].tolist() == [5 * 60_000.0]
    assert bars[0].tolist() == [300_000.0, 105.0, 111.0, 104.0, 110.0, 85.0]


def test_a_hole_inside_a_covered_bucket_still_raises():
    covered = np.delete(_minute_rows(12)[2:], 4, axis=0)  # drops 06:00 from bucket 1

    with pytest.raises(ValueError, match="incomplete higher-timeframe source coverage"):
        confirmed_higher_timeframe_bars(
            covered,
            source_timeframe="1m",
            target_timeframe="5m",
            decision_time_ms=12 * 60_000,
        )
