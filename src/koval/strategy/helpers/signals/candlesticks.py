from __future__ import annotations


def is_hammer(
    open_: float,
    high: float,
    low: float,
    close: float,
    body_ratio: float = 2.0,
) -> bool:
    """Hammer: long lower wick (>= body_ratio * body), small upper wick (<= 50% body)."""
    body = abs(close - open_)
    if body == 0.0:
        return False
    lower_wick = min(open_, close) - low
    upper_wick = high - max(open_, close)
    return lower_wick >= body * body_ratio and upper_wick <= body * 0.5


def is_shooting_star(
    open_: float,
    high: float,
    low: float,
    close: float,
    body_ratio: float = 2.0,
) -> bool:
    """Shooting Star: long upper wick (>= body_ratio * body), small lower wick (<= 50% body)."""
    body = abs(close - open_)
    if body == 0.0:
        return False
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low
    return upper_wick >= body * body_ratio and lower_wick <= body * 0.5


def is_bullish_engulfing(
    prev_open: float,
    prev_close: float,
    curr_open: float,
    curr_close: float,
) -> bool:
    """Bullish Engulfing: previous bearish, current bullish and fully engulfs previous body."""
    prev_bearish = prev_close < prev_open
    curr_bullish = curr_close > curr_open
    engulfs = curr_open <= prev_close and curr_close >= prev_open
    return prev_bearish and curr_bullish and engulfs


def is_bearish_engulfing(
    prev_open: float,
    prev_close: float,
    curr_open: float,
    curr_close: float,
) -> bool:
    """Bearish Engulfing: previous bullish, current bearish and fully engulfs previous body."""
    prev_bullish = prev_close > prev_open
    curr_bearish = curr_close < curr_open
    engulfs = curr_open >= prev_close and curr_close <= prev_open
    return prev_bullish and curr_bearish and engulfs


def is_doji(
    open_: float,
    high: float,
    low: float,
    close: float,
    body_pct: float = 0.1,
) -> bool:
    """Doji: body is <= body_pct of total range. Returns False if total range is zero."""
    total_range = high - low
    if total_range == 0.0:
        return False
    body = abs(close - open_)
    return (body / total_range) <= body_pct
