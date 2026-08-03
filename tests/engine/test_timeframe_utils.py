"""Unit tests for koval.engine.timeframe_utils."""

from koval.engine.timeframe_utils import ordered_timeframes, timeframe_to_minutes


class TestTimeframeToMinutes:
    def test_timeframe_to_minutes_15m_returns_15(self):
        assert timeframe_to_minutes("15m") == 15

    def test_timeframe_to_minutes_1h_returns_60(self):
        assert timeframe_to_minutes("1h") == 60

    def test_timeframe_to_minutes_4h_returns_240(self):
        assert timeframe_to_minutes("4h") == 240

    def test_timeframe_to_minutes_1d_returns_1440(self):
        assert timeframe_to_minutes("1d") == 1440

    def test_timeframe_to_minutes_1w_returns_10080(self):
        assert timeframe_to_minutes("1w") == 10080

    def test_timeframe_to_minutes_empty_returns_large_value(self):
        assert timeframe_to_minutes("") == 10**9

    def test_timeframe_to_minutes_invalid_returns_large_value(self):
        assert timeframe_to_minutes("invalid") == 10**9


class TestOrderedTimeframes:
    def test_ordered_timeframes_sorts_low_to_high(self):
        assert ordered_timeframes(["4h", "15m"]) == ["15m", "4h"]

    def test_ordered_timeframes_three_frames(self):
        assert ordered_timeframes(["1d", "15m", "1h"]) == ["15m", "1h", "1d"]

    def test_ordered_timeframes_empty_defaults_to_1h(self):
        assert ordered_timeframes([]) == ["1h"]

    def test_ordered_timeframes_none_defaults_to_1h(self):
        assert ordered_timeframes(None) == ["1h"]
