import numpy as np

import koval.engine.trade_indicators as trade_indicators
from koval.engine.trade_indicators import (
    _adx_value,
    adx_series,
    ema_series,
    indicators_at,
    rsi_series,
)
from koval.strategy.helpers._math import _rsi


def _rising(n=120):
    return np.arange(1.0, 1.0 + n, dtype=float)


def test_ema_series_matches_input_length():
    closes = _rising()

    out = ema_series(closes, period=10)

    assert len(out) == len(closes)
    assert all(np.isfinite(out))


def test_rsi_series_high_on_monotonic_uptrend():
    closes = _rising()

    out = rsi_series(closes, period=14)

    assert len(out) == len(closes)
    assert out[-1] > 70.0


def test_rsi_series_matches_scalar_reference_for_each_bar():
    n = 96
    closes = np.linspace(100.0, 126.0, n) + np.sin(np.arange(n) / 3.0) * 2.5

    out = rsi_series(closes, period=14)

    expected = [float(_rsi(closes[: i + 1], 14)) if i + 1 > 14 else 50.0 for i in range(n)]
    assert np.allclose(out, expected)


def test_rsi_series_does_not_recompute_scalar_value_for_every_prefix(monkeypatch):
    closes = _rising(96)

    def fail_scalar_recompute(*_args, **_kwargs):
        raise AssertionError("rsi_series must compute the full series in one pass")

    monkeypatch.setattr(trade_indicators, "_rsi", fail_scalar_recompute)

    out = rsi_series(closes, period=14)

    assert len(out) == len(closes)
    assert out[-1] > 70.0


def test_adx_series_length_and_finite():
    n = 120
    highs = _rising(n) + 1
    lows = _rising(n) - 1
    closes = _rising(n)

    out = adx_series(highs, lows, closes, period=14)

    assert len(out) == n
    assert np.isfinite(out[-1])


def test_adx_series_matches_scalar_reference_for_each_bar():
    n = 96
    base = np.linspace(100.0, 126.0, n)
    closes = base + np.sin(np.arange(n) / 3.0) * 2.5
    highs = closes + 1.0 + np.cos(np.arange(n) / 5.0)
    lows = closes - 1.2 - np.sin(np.arange(n) / 7.0)

    out = adx_series(highs, lows, closes, period=14)

    expected = [_adx_value(highs[: i + 1], lows[: i + 1], closes[: i + 1], 14) for i in range(n)]
    assert np.allclose(out, expected)


def test_adx_series_does_not_recompute_scalar_value_for_every_prefix(monkeypatch):
    n = 96
    closes = _rising(n)
    highs = closes + 1
    lows = closes - 1

    def fail_scalar_recompute(*_args, **_kwargs):
        raise AssertionError("adx_series must compute the full series in one pass")

    monkeypatch.setattr(trade_indicators, "_adx_value", fail_scalar_recompute)

    out = adx_series(highs, lows, closes, period=14)

    assert len(out) == n
    assert np.isfinite(out[-1])


def test_indicators_at_returns_scalar_snapshot():
    n = 120
    closes = _rising(n)
    highs = closes + 1
    lows = closes - 1

    snap = indicators_at(
        closes,
        highs,
        lows,
        index=n - 1,
        rsi_period=14,
        ema_period=20,
        adx_period=14,
    )

    assert "RSI" in snap and "EMA" in snap and "ADX" in snap
    assert all(np.isfinite(v) for v in snap.values())
