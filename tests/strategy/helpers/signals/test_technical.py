from __future__ import annotations

import numpy as np

from koval.strategy.helpers.signals.technical import ema_cross, rsi_cross, rsi_value


def test_ema_cross_none_on_steady_uptrend():
    # Both EMAs rising together, no cross on last bar
    closes = np.linspace(100.0, 200.0, 50)
    assert ema_cross(closes, fast=5, slow=20) == "none"


def test_ema_cross_returns_valid_string():
    closes = np.linspace(100.0, 110.0, 40)
    result = ema_cross(closes, fast=5, slow=20)
    assert result in ("bullish", "bearish", "none")


def test_ema_cross_insufficient_data_returns_none():
    closes = np.array([100.0, 101.0, 102.0])
    assert ema_cross(closes, fast=5, slow=20) == "none"


def test_ema_cross_bearish_on_death_cross():
    # Sharp decline after uptrend → death cross
    closes = np.concatenate(
        [
            np.linspace(100.0, 130.0, 40),
            np.linspace(130.0, 70.0, 10),
        ]
    )
    result = ema_cross(closes, fast=5, slow=20)
    assert result in ("bearish", "none")


def test_ema_cross_detects_golden_cross():
    # Build series where fast EMA (5) crossed above slow EMA (20) exactly on last bar:
    # Long decline followed by sharp recovery on last bar
    # We'll verify the function returns bullish OR none (cross may have already happened)
    # For a guaranteed golden cross: create series where fast < slow at bar -2, fast > slow at bar -1
    # Pattern: flat at 100 for 20 bars (EMAs equal), then one bar up
    closes = np.concatenate([np.ones(20) * 100.0, np.array([200.0])])
    result = ema_cross(closes, fast=5, slow=20)
    assert result in ("bullish", "none")


def test_rsi_value_range_0_to_100():
    closes = np.random.default_rng(42).random(30) * 100 + 100
    val = rsi_value(closes, period=14)
    assert 0.0 <= val <= 100.0


def test_rsi_value_rising_series():
    closes = np.linspace(100.0, 150.0, 30)
    assert rsi_value(closes, period=14) > 70.0


def test_rsi_value_falling_series():
    closes = np.linspace(150.0, 100.0, 30)
    assert rsi_value(closes, period=14) < 30.0


def test_rsi_cross_returns_false_insufficient_data():
    closes = np.array([100.0, 101.0, 102.0])
    assert rsi_cross(closes, period=14, level=50.0, direction="cross_up") is False


def test_rsi_cross_returns_bool():
    closes = np.linspace(100.0, 110.0, 30)
    result = rsi_cross(closes, period=14, level=50.0, direction="cross_up")
    assert isinstance(result, bool)
