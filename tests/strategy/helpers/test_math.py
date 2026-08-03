from __future__ import annotations

import numpy as np
import pytest

from koval.strategy.helpers._math import _atr, _ema, _macd, _rsi, _true_range


def test_ema_length_matches_input():
    closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    result = _ema(closes, period=3)
    assert len(result) == len(closes)


def test_ema_converges_on_constant_series():
    closes = np.ones(50) * 100.0
    result = _ema(closes, period=10)
    assert result[-1] == pytest.approx(100.0, rel=1e-6)


def test_ema_rising_series_above_mean():
    closes = np.arange(1.0, 21.0)
    result = _ema(closes, period=5)
    assert result[-1] > closes[:5].mean()


def test_rsi_flat_series_returns_near_50():
    closes = np.ones(30) * 100.0
    val = _rsi(closes, period=14)
    assert val == pytest.approx(50.0)


def test_rsi_rising_series_returns_high():
    closes = np.linspace(100.0, 200.0, 30)
    val = _rsi(closes, period=14)
    assert val > 70.0


def test_rsi_falling_series_returns_low():
    closes = np.linspace(200.0, 100.0, 30)
    val = _rsi(closes, period=14)
    assert val < 30.0


def test_rsi_insufficient_data_returns_50():
    closes = np.array([100.0, 101.0])
    assert _rsi(closes, period=14) == pytest.approx(50.0)


def test_true_range_single_bar_no_prev():
    highs = np.array([105.0])
    lows = np.array([95.0])
    closes = np.array([100.0])
    tr = _true_range(highs, lows, closes)
    assert tr[0] == pytest.approx(10.0)


def test_true_range_gap_up():
    highs = np.array([100.0, 120.0])
    lows = np.array([90.0, 115.0])
    closes = np.array([100.0, 118.0])
    tr = _true_range(highs, lows, closes)
    assert tr[1] == pytest.approx(20.0)


def test_atr_returns_positive():
    n = 20
    highs = np.linspace(105.0, 125.0, n)
    lows = np.linspace(95.0, 115.0, n)
    closes = np.linspace(100.0, 120.0, n)
    val = _atr(highs, lows, closes, period=14)
    assert val > 0.0


def test_atr_insufficient_data_returns_zero():
    highs = np.array([105.0])
    lows = np.array([95.0])
    closes = np.array([100.0])
    assert _atr(highs, lows, closes, period=14) == 0.0


def test_macd_returns_three_floats():
    closes = np.linspace(100.0, 130.0, 60)
    macd_val, sig_val, hist = _macd(closes)
    assert isinstance(macd_val, float)
    assert isinstance(sig_val, float)
    assert isinstance(hist, float)


def test_macd_insufficient_data_returns_zeros():
    closes = np.array([100.0, 101.0])
    assert _macd(closes) == (0.0, 0.0, 0.0)


def test_rsi_known_value():
    # 15 bars: first 14 deltas all +1.0 (14 gains, 0 losses). RSI should be 100.
    closes = np.arange(1.0, 16.0)  # [1, 2, ..., 15]
    val = _rsi(closes, period=14)
    assert val == pytest.approx(100.0)


def test_atr_constant_true_range():
    # Each bar: high = close + 5, low = close - 5, so TR = 10 for all bars
    n = 30
    closes = np.linspace(100.0, 110.0, n)
    highs = closes + 5.0
    lows = closes - 5.0
    # With Wilder smoothing on a constant TR series, ATR converges to 10.0
    val = _atr(highs, lows, closes, period=14)
    assert val == pytest.approx(10.0, rel=1e-3)
