"""Batch windows must reproduce the existing scalar helpers exactly."""

import numpy as np
import pytest

from koval.strategy.helpers._math import _atr, _ema, _rsi
from koval.strategy.helpers.rolling_indicators import rolling_atr, rolling_ema, rolling_rsi


def prices(count):
    rng = np.random.default_rng(37)
    closes = 100 + rng.normal(0, 3, count).cumsum()
    return closes + rng.uniform(0, 5, count), closes - rng.uniform(0, 5, count), closes


@pytest.mark.parametrize("window", [2, 15, 31, 1000])
@pytest.mark.parametrize("period", [2, 14, 31])
def test_every_window_matches_scalar_helpers_bit_for_bit(window, period):
    highs, lows, closes = prices(window + 21)
    actual_ema = rolling_ema(closes, period, window)
    actual_rsi = rolling_rsi(closes, period, window)
    actual_atr = rolling_atr(highs, lows, closes, period, window)
    for end in range(1, len(closes) + 1):
        start = max(0, end - window)
        c = closes[start:end]
        ema = _ema(c, period)
        np.testing.assert_array_equal(
            actual_ema[end - 1], [ema[-2] if len(ema) > 1 else np.nan, ema[-1]]
        )
        np.testing.assert_array_equal(actual_rsi[end - 1], [_rsi(c[:-1], period), _rsi(c, period)])
        assert actual_atr[end - 1] == _atr(highs[start:end], lows[start:end], c, period)


@pytest.mark.parametrize("shape", ["flat", "rising", "falling", "alternating"])
def test_zero_gains_losses_and_threshold_equality(shape):
    closes = {
        "flat": np.full(77, 100.0),
        "rising": np.arange(77, dtype=float) + 100,
        "falling": 200 - np.arange(77, dtype=float),
        "alternating": np.tile([100.0, 101.0], 39)[:77],
    }[shape]
    pairs = rolling_rsi(closes, 14, 31)
    for end in range(1, len(closes) + 1):
        c = closes[max(0, end - 31) : end]
        np.testing.assert_array_equal(pairs[end - 1], [_rsi(c[:-1]), _rsi(c)])


def test_previous_value_uses_current_window_seed():
    _, _, closes = prices(100)
    pairs = rolling_ema(closes, 14, 31)
    assert pairs[60, 0] != pairs[59, 1]
    assert pairs[60, 0] == _ema(closes[30:60], 14)[-1]


def test_future_candles_cannot_change_prepared_past():
    highs, lows, closes = prices(120)
    changed = closes.copy()
    changed[80:] *= 3
    np.testing.assert_array_equal(
        rolling_ema(closes, 14, 31)[:80], rolling_ema(changed, 14, 31)[:80]
    )
    np.testing.assert_array_equal(
        rolling_rsi(closes, 14, 31)[:80], rolling_rsi(changed, 14, 31)[:80]
    )
    np.testing.assert_array_equal(
        rolling_atr(highs, lows, closes, 14, 31)[:80],
        rolling_atr(highs * 1, lows * 1, changed, 14, 31)[:80],
    )


def test_empty_inputs_and_multiple_tiles():
    empty = np.array([])
    assert rolling_ema(empty, 14, 31).shape == (0, 2)
    assert rolling_rsi(empty, 14, 31).shape == (0, 2)
    assert rolling_atr(empty, empty, empty, 14, 31).shape == (0,)
    highs, lows, closes = prices(9000)
    for fn, args in (
        (rolling_ema, (closes,)),
        (rolling_rsi, (closes,)),
        (rolling_atr, (highs, lows, closes)),
    ):
        values = fn(*args, 14, 31)
        for end in (4095, 4096, 4097, 8192, 9000):
            sliced = [a[end - 31 : end] for a in args]
            expected = (
                _ema(sliced[0], 14)[-2:]
                if fn is rolling_ema
                else [_rsi(sliced[0][:-1]), _rsi(sliced[0])]
                if fn is rolling_rsi
                else _atr(*sliced, 14)
            )
            np.testing.assert_array_equal(values[end - 1], expected)
