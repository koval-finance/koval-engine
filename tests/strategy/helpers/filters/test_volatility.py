from __future__ import annotations

import numpy as np

from koval.strategy.helpers.filters.volatility import atr_volatility_filter, bb_volatility_filter


def test_atr_filter_passes_with_high_volatility():
    n = 20
    closes = np.linspace(100.0, 110.0, n)
    highs = closes + 5.0
    lows = closes - 5.0
    assert atr_volatility_filter(highs, lows, closes, period=14, min_atr_pct=1.0) is True


def test_atr_filter_rejects_low_volatility():
    n = 20
    closes = np.ones(n) * 100.0
    highs = closes + 0.01
    lows = closes - 0.01
    assert atr_volatility_filter(highs, lows, closes, period=14, min_atr_pct=1.0) is False


def test_atr_filter_insufficient_data_returns_false():
    highs = np.array([105.0])
    lows = np.array([95.0])
    closes = np.array([100.0])
    assert atr_volatility_filter(highs, lows, closes, period=14, min_atr_pct=0.5) is False


def test_atr_filter_returns_bool():
    n = 20
    closes = np.linspace(100.0, 110.0, n)
    assert isinstance(atr_volatility_filter(closes + 2.0, closes - 2.0, closes), bool)


def test_bb_filter_passes_with_wide_bands():
    closes = np.random.default_rng(0).standard_normal(30) * 10 + 100
    assert bb_volatility_filter(closes, period=20, min_bandwidth_pct=1.0) is True


def test_bb_filter_rejects_with_narrow_bands():
    closes = np.ones(30) * 100.0
    assert bb_volatility_filter(closes, period=20, min_bandwidth_pct=1.0) is False


def test_bb_filter_insufficient_data_returns_false():
    closes = np.array([100.0, 101.0])
    assert bb_volatility_filter(closes, period=20, min_bandwidth_pct=1.0) is False


def test_bb_filter_returns_bool():
    closes = np.linspace(95.0, 105.0, 30)
    assert isinstance(bb_volatility_filter(closes, period=20, min_bandwidth_pct=1.0), bool)
