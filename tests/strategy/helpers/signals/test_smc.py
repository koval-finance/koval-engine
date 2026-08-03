from __future__ import annotations

import numpy as np
import pytest

from koval.strategy.helpers.signals.smc import detect_bos, detect_choch, detect_fvg, detect_ob

# --- BOS ---


def test_bos_bullish_when_close_breaks_swing_high():
    highs = np.array([105.0, 108.0, 110.0, 107.0, 106.0, 115.0])
    lows = np.array([95.0, 96.0, 98.0, 97.0, 96.0, 112.0])
    closes = np.array([100.0, 104.0, 109.0, 103.0, 102.0, 115.0])
    assert detect_bos(highs, lows, closes, lookback=5) == "bullish"


def test_bos_bearish_when_close_breaks_swing_low():
    highs = np.array([105.0, 104.0, 106.0, 103.0, 104.0, 90.0])
    lows = np.array([95.0, 94.0, 93.0, 92.0, 93.0, 85.0])
    closes = np.array([100.0, 98.0, 96.0, 94.0, 95.0, 85.0])
    assert detect_bos(highs, lows, closes, lookback=5) == "bearish"


def test_bos_none_when_no_break():
    highs = np.array([105.0, 106.0, 107.0, 108.0, 107.0, 106.0])
    lows = np.array([95.0, 96.0, 97.0, 98.0, 97.0, 96.0])
    closes = np.array([100.0, 101.0, 102.0, 103.0, 102.0, 101.0])
    assert detect_bos(highs, lows, closes, lookback=5) == "none"


def test_bos_insufficient_data_returns_none():
    highs = np.array([105.0, 106.0])
    lows = np.array([95.0, 96.0])
    closes = np.array([100.0, 101.0])
    assert detect_bos(highs, lows, closes, lookback=5) == "none"


# --- CHoCH ---


def test_choch_bearish_reversal_in_uptrend():
    # The four prior bars trend upward from 100 to 106, then the current
    # valid candle closes below their 99 swing low.
    closes = np.array([99.0, 100.0, 102.0, 104.0, 106.0, 94.0])
    highs = np.array([101.0, 102.0, 104.0, 106.0, 108.0, 96.0])
    lows = np.array([98.0, 99.0, 101.0, 103.0, 105.0, 93.0])
    result = detect_choch(highs, lows, closes, lookback=4)
    assert result == "bearish"


def test_choch_bullish_reversal_in_downtrend():
    # The four prior bars trend downward from 110 to 104, then the current
    # valid candle closes above their 111 swing high.
    closes = np.array([111.0, 110.0, 108.0, 106.0, 104.0, 116.0])
    highs = np.array([112.0, 111.0, 109.0, 107.0, 105.0, 117.0])
    lows = np.array([110.0, 109.0, 107.0, 105.0, 103.0, 115.0])
    result = detect_choch(highs, lows, closes, lookback=4)
    assert result == "bullish"


def test_choch_none_on_flat_market():
    highs = np.array([101.0, 101.0, 101.0, 101.0, 101.0])
    lows = np.array([99.0, 99.0, 99.0, 99.0, 99.0])
    closes = np.array([100.0, 100.0, 100.0, 100.0, 100.0])
    assert detect_choch(highs, lows, closes, lookback=4) == "none"


# --- FVG ---


def test_fvg_bullish_gap_detected():
    highs = np.array([100.0, 105.0, 108.0])
    lows = np.array([95.0, 102.0, 103.0])
    result = detect_fvg(highs, lows)
    assert result is not None
    gap_low, gap_high = result
    assert gap_low == pytest.approx(100.0)
    assert gap_high == pytest.approx(103.0)


def test_fvg_bearish_gap_detected():
    highs = np.array([105.0, 98.0, 97.0])
    lows = np.array([100.0, 94.0, 90.0])
    result = detect_fvg(highs, lows)
    assert result is not None
    gap_low, gap_high = result
    assert gap_low == pytest.approx(97.0)
    assert gap_high == pytest.approx(100.0)


def test_fvg_no_gap_returns_none():
    highs = np.array([105.0, 108.0, 106.0])
    lows = np.array([95.0, 102.0, 101.0])
    assert detect_fvg(highs, lows) is None


def test_fvg_insufficient_data_returns_none():
    assert detect_fvg(np.array([100.0]), np.array([95.0])) is None


# --- OB ---


def test_ob_bullish_finds_last_bearish_candle():
    opens = np.array([100.0, 102.0, 98.0, 95.0, 97.0, 110.0])
    highs = np.array([105.0, 106.0, 102.0, 99.0, 101.0, 115.0])
    lows = np.array([98.0, 99.0, 94.0, 92.0, 94.0, 108.0])
    closes = np.array([102.0, 104.0, 96.0, 96.0, 99.0, 113.0])
    result = detect_ob(opens, highs, lows, closes, lookback=5)
    assert result is not None
    ob_low, ob_high, direction = result
    assert direction == "bullish"
    assert ob_low < ob_high


def test_ob_insufficient_data_returns_none():
    opens = np.array([100.0])
    highs = np.array([105.0])
    lows = np.array([95.0])
    closes = np.array([102.0])
    assert detect_ob(opens, highs, lows, closes, lookback=5) is None
