from __future__ import annotations

import numpy as np

from koval.strategy.helpers.filters.trend import adx_filter, ema_trend_filter


def test_adx_filter_passes_on_strong_trend():
    n = 60  # more bars for Wilder smoothing to stabilize
    closes = np.linspace(100.0, 160.0, n)
    highs = closes + 2.0
    lows = closes - 2.0
    assert adx_filter(highs, lows, closes, period=14, min_adx=20.0) is True


def test_adx_filter_insufficient_data_returns_false():
    # Need period*2+1 bars; 2 bars is clearly insufficient
    highs = np.array([105.0, 106.0])
    lows = np.array([95.0, 96.0])
    closes = np.array([100.0, 101.0])
    assert adx_filter(highs, lows, closes, period=14, min_adx=25.0) is False


def test_adx_filter_returns_bool():
    n = 40
    closes = np.ones(n) * 100.0
    highs = closes + 1.0
    lows = closes - 1.0
    assert isinstance(adx_filter(highs, lows, closes, period=14, min_adx=25.0), bool)


def test_ema_trend_filter_bullish_when_above_ema():
    closes = np.concatenate([np.ones(50) * 100.0, np.ones(10) * 150.0])
    assert ema_trend_filter(closes, period=20, direction="bullish") is True


def test_ema_trend_filter_bearish_when_below_ema():
    closes = np.concatenate([np.ones(50) * 150.0, np.ones(10) * 100.0])
    assert ema_trend_filter(closes, period=20, direction="bearish") is True


def test_ema_trend_filter_bullish_rejects_when_below_ema():
    closes = np.concatenate([np.ones(50) * 150.0, np.ones(10) * 100.0])
    assert ema_trend_filter(closes, period=20, direction="bullish") is False


def test_ema_trend_filter_insufficient_data_returns_false():
    closes = np.array([100.0, 101.0, 102.0])
    assert ema_trend_filter(closes, period=200, direction="bullish") is False


def test_ema_trend_filter_returns_bool():
    closes = np.linspace(100.0, 110.0, 30)
    assert isinstance(ema_trend_filter(closes, period=20, direction="bullish"), bool)
