from __future__ import annotations

import numpy as np

from koval.strategy.helpers.filters.momentum import macd_filter, rsi_filter, stoch_filter


def test_rsi_filter_passes_when_in_range():
    # Mixed up and down moves to produce RSI in the 40-80 range
    closes = np.array(
        [
            100.0,
            101.0,
            100.5,
            101.5,
            102.0,
            101.0,
            102.5,
            103.0,
            102.0,
            103.5,
            104.0,
            103.0,
            104.5,
            105.0,
            104.0,
            105.5,
            106.0,
            105.0,
            106.5,
            107.0,
            106.0,
            107.5,
            108.0,
            107.0,
            108.5,
            109.0,
            108.0,
            109.5,
            110.0,
            109.0,
        ]
    )
    assert rsi_filter(closes, period=14, min_val=40.0, max_val=80.0) is True


def test_rsi_filter_rejects_when_overbought():
    closes = np.linspace(100.0, 200.0, 30)
    assert rsi_filter(closes, period=14, min_val=0.0, max_val=70.0) is False


def test_rsi_filter_rejects_when_oversold():
    closes = np.linspace(200.0, 100.0, 30)
    assert rsi_filter(closes, period=14, min_val=40.0, max_val=100.0) is False


def test_rsi_filter_returns_bool():
    closes = np.ones(20) * 100.0
    assert isinstance(rsi_filter(closes), bool)


def test_stoch_filter_passes_near_midrange():
    n = 20
    highs = np.linspace(105.0, 115.0, n)
    lows = np.linspace(95.0, 105.0, n)
    closes = np.linspace(100.0, 110.0, n)
    assert stoch_filter(highs, lows, closes, k_period=14, min_val=20.0, max_val=80.0) is True


def test_stoch_filter_rejects_at_top():
    n = 20
    highs = np.linspace(105.0, 115.0, n)
    lows = np.linspace(95.0, 105.0, n)
    closes = highs.copy()
    assert stoch_filter(highs, lows, closes, k_period=14, min_val=0.0, max_val=80.0) is False


def test_stoch_filter_returns_bool():
    n = 20
    h = np.ones(n) * 105.0
    lo = np.ones(n) * 95.0
    c = np.ones(n) * 100.0
    assert isinstance(stoch_filter(h, lo, c), bool)


def test_macd_filter_passes_on_bullish_histogram():
    # Use mixed data: stable then ramp to create MACD divergence
    closes = np.concatenate([np.ones(20) * 100.0, np.linspace(100.0, 160.0, 40)])
    assert macd_filter(closes, require_positive=True) is True


def test_macd_filter_passes_on_bearish_histogram():
    # Use mixed data: stable then decline to create bearish MACD
    closes = np.concatenate([np.ones(20) * 160.0, np.linspace(160.0, 100.0, 40)])
    assert macd_filter(closes, require_positive=False) is True


def test_macd_filter_rejects_wrong_direction():
    # Bullish data with bearish requirement should fail
    closes = np.concatenate([np.ones(20) * 100.0, np.linspace(100.0, 160.0, 40)])
    assert macd_filter(closes, require_positive=False) is False


def test_macd_filter_insufficient_data_returns_false():
    closes = np.array([100.0, 101.0])
    assert macd_filter(closes) is False


def test_rsi_filter_insufficient_data_returns_false():
    closes = np.array([100.0, 101.0, 102.0])
    assert rsi_filter(closes, period=14) is False
