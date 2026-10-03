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


def _flat_range(half_range, n=20):
    # Constant true range 2 * half_range on a flat 100 close: atr_pct == 2 * half_range.
    closes = np.full(n, 100.0)
    return closes + half_range, closes - half_range, closes


def test_atr_filter_rejects_above_the_cap():
    highs, lows, closes = _flat_range(1.0)
    assert atr_volatility_filter(highs, lows, closes, min_atr_pct=0.0, max_atr_pct=1.5) is False


def test_atr_filter_passes_at_the_cap():
    highs, lows, closes = _flat_range(1.0)
    assert atr_volatility_filter(highs, lows, closes, min_atr_pct=0.0, max_atr_pct=2.0) is True


def test_atr_filter_band_passes_inside_and_rejects_both_sides():
    band = {"min_atr_pct": 1.0, "max_atr_pct": 3.0}
    assert atr_volatility_filter(*_flat_range(1.0), **band) is True
    assert atr_volatility_filter(*_flat_range(0.25), **band) is False
    assert atr_volatility_filter(*_flat_range(2.0), **band) is False


def test_atr_filter_cap_only_still_rejects_zero_atr():
    closes = np.full(20, 100.0)
    assert atr_volatility_filter(closes, closes, closes, min_atr_pct=0.0, max_atr_pct=5.0) is False
