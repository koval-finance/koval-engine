from __future__ import annotations

import numpy as np


def detect_bos(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    lookback: int = 10,
) -> str:
    """
    Break of Structure detection.
    Returns 'bullish' if current close breaks above recent swing high,
    'bearish' if below recent swing low, 'none' otherwise.
    """
    if len(highs) < lookback + 1:
        return "none"
    swing_high = highs[-(lookback + 1) : -1].max()
    swing_low = lows[-(lookback + 1) : -1].min()
    current = closes[-1]
    if current > swing_high:
        return "bullish"
    if current < swing_low:
        return "bearish"
    return "none"


def detect_choch(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    lookback: int = 5,
) -> str:
    """
    Change of Character — potential trend reversal signal.
    Detects when price breaks structure AGAINST the established trend.
    Returns 'bullish' (downtrend breaking up), 'bearish' (uptrend breaking down), or 'none'.
    """
    if len(closes) < lookback + 2:
        return "none"
    # Establish trend from completed bars before evaluating the current bar as
    # a possible reversal. Including the current close in both decisions makes
    # the break condition impossible for valid OHLC data.
    trend_start = closes[-(lookback + 1)]
    trend_end = closes[-2]
    current = float(closes[-1])
    if trend_end == trend_start:
        return "none"
    is_uptrend = float(trend_end) > float(trend_start)
    swing_high = highs[-(lookback + 1) : -1].max()
    swing_low = lows[-(lookback + 1) : -1].min()
    # CHoCH in uptrend = bearish break (close breaks below swing low — reversal)
    if is_uptrend and current < swing_low:
        return "bearish"
    # CHoCH in downtrend = bullish break (close breaks above swing high — reversal)
    if not is_uptrend and current > swing_high:
        return "bullish"
    return "none"


def detect_fvg(
    highs: np.ndarray,
    lows: np.ndarray,
) -> tuple[float, float] | None:
    """
    Fair Value Gap on last 3 bars.
    Bullish FVG: high[i-2] < low[i] — gap between them.
    Bearish FVG: low[i-2] > high[i] — gap between them.
    Returns (gap_low, gap_high) or None.
    """
    if len(highs) < 3:
        return None
    h0, h2 = float(highs[-3]), float(highs[-1])
    l0, l2 = float(lows[-3]), float(lows[-1])
    if h0 < l2:
        return (h0, l2)
    if l0 > h2:
        return (h2, l0)
    return None


def detect_ob(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    lookback: int = 10,
) -> tuple[float, float, str] | None:
    """
    Order Block detection.
    Bullish OB: last bearish candle (close < open) before current bullish context.
    Bearish OB: last bullish candle (close > open) before current bearish context.
    Returns (ob_low, ob_high, 'bullish'|'bearish') or None.
    """
    if len(opens) < lookback + 1:
        return None
    avg = closes[-lookback:].mean()
    current = float(closes[-1])
    is_bullish_ctx = current > avg

    search_start = len(opens) - 2
    search_end = max(len(opens) - lookback - 2, -1)  # -1 makes range include index 0

    for i in range(search_start, search_end, -1):
        if is_bullish_ctx and closes[i] < opens[i]:
            return (float(lows[i]), float(highs[i]), "bullish")
        if not is_bullish_ctx and closes[i] > opens[i]:
            return (float(lows[i]), float(highs[i]), "bearish")
    return None
