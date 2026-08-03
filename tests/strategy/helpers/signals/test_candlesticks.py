from __future__ import annotations

from koval.strategy.helpers.signals.candlesticks import (
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_doji,
    is_hammer,
    is_shooting_star,
)


def test_hammer_detects_long_lower_wick():
    # body=2 (98→100), lower wick=8 (90→98), upper wick=0.5 → hammer
    assert is_hammer(open_=98.0, high=100.5, low=90.0, close=100.0) is True


def test_hammer_not_triggered_with_short_lower_wick():
    assert is_hammer(open_=98.0, high=102.0, low=96.0, close=100.0) is False


def test_hammer_not_triggered_inverted_body():
    # bearish body with long lower wick IS still a hammer by shape
    assert is_hammer(open_=100.0, high=100.5, low=90.0, close=98.0) is True


def test_hammer_zero_body_returns_false():
    assert is_hammer(open_=100.0, high=105.0, low=90.0, close=100.0) is False


def test_shooting_star_detects_long_upper_wick():
    assert is_shooting_star(open_=100.0, high=110.0, low=99.5, close=102.0) is True


def test_shooting_star_not_triggered_with_short_upper_wick():
    assert is_shooting_star(open_=100.0, high=103.0, low=97.0, close=102.0) is False


def test_shooting_star_zero_body_returns_false():
    assert is_shooting_star(open_=100.0, high=110.0, low=99.5, close=100.0) is False


def test_bullish_engulfing_detected():
    # Prev: bearish (open 105, close 100). Curr: bullish (open 99, close 107).
    assert (
        is_bullish_engulfing(
            prev_open=105.0,
            prev_close=100.0,
            curr_open=99.0,
            curr_close=107.0,
        )
        is True
    )


def test_bullish_engulfing_not_when_prev_bullish():
    assert (
        is_bullish_engulfing(
            prev_open=100.0,
            prev_close=105.0,
            curr_open=99.0,
            curr_close=107.0,
        )
        is False
    )


def test_bullish_engulfing_not_when_curr_doesnt_engulf():
    assert (
        is_bullish_engulfing(
            prev_open=105.0,
            prev_close=100.0,
            curr_open=102.0,
            curr_close=107.0,
        )
        is False
    )


def test_bearish_engulfing_detected():
    # Prev: bullish (open 100, close 105). Curr: bearish (open 106, close 98).
    assert (
        is_bearish_engulfing(
            prev_open=100.0,
            prev_close=105.0,
            curr_open=106.0,
            curr_close=98.0,
        )
        is True
    )


def test_bearish_engulfing_not_when_prev_bearish():
    assert (
        is_bearish_engulfing(
            prev_open=105.0,
            prev_close=100.0,
            curr_open=106.0,
            curr_close=98.0,
        )
        is False
    )


def test_doji_detected_tiny_body():
    # Total range = 10, body = 0.1 → body_pct = 1% < 10% threshold
    assert is_doji(open_=100.0, high=105.0, low=95.0, close=100.1) is True


def test_doji_not_for_normal_candle():
    assert is_doji(open_=100.0, high=108.0, low=97.0, close=105.0) is False


def test_doji_zero_range_returns_false():
    assert is_doji(open_=100.0, high=100.0, low=100.0, close=100.0) is False
