"""Recompute indicator series and snapshots from OHLCV arrays."""

from __future__ import annotations

import numpy as np

from koval.strategy.helpers._math import _ema, _rsi, _true_range


def ema_series(closes: np.ndarray, period: int) -> list[float]:
    """Return an EMA series aligned one-for-one with closes."""
    closes = np.asarray(closes, dtype=float)
    if len(closes) == 0:
        return []
    values = _ema(closes, period)
    finite_values = np.where(np.isfinite(values), values, closes)
    return [float(v) for v in finite_values]


def rsi_series(closes: np.ndarray, period: int = 14) -> list[float]:
    """Return an RSI series aligned one-for-one with closes."""
    closes = np.asarray(closes, dtype=float)
    out = [50.0] * len(closes)
    if len(closes) <= period:
        return out

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())

    def current_rsi() -> float:
        if avg_gain == 0.0 and avg_loss == 0.0:
            return 50.0
        if avg_loss == 0.0:
            return 100.0
        return float(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))

    out[period] = current_rsi()
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
        out[i + 1] = current_rsi()
    return out


def _adx_value(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
    if len(closes) < period * 2 + 1:
        return 0.0

    tr = _true_range(highs, lows, closes)
    n = len(closes)
    dm_plus = np.zeros(n)
    dm_minus = np.zeros(n)
    for i in range(1, n):
        up = float(highs[i]) - float(highs[i - 1])
        down = float(lows[i - 1]) - float(lows[i])
        if up > down and up > 0:
            dm_plus[i] = up
        if down > up and down > 0:
            dm_minus[i] = down

    smooth_tr = float(tr[1 : period + 1].sum())
    smooth_plus = float(dm_plus[1 : period + 1].sum())
    smooth_minus = float(dm_minus[1 : period + 1].sum())

    dx_values: list[float] = []
    for i in range(period + 1, n):
        smooth_tr = smooth_tr - smooth_tr / period + float(tr[i])
        smooth_plus = smooth_plus - smooth_plus / period + float(dm_plus[i])
        smooth_minus = smooth_minus - smooth_minus / period + float(dm_minus[i])
        if smooth_tr == 0.0:
            dx_values.append(0.0)
            continue
        di_plus = 100.0 * smooth_plus / smooth_tr
        di_minus = 100.0 * smooth_minus / smooth_tr
        denom = di_plus + di_minus
        dx_values.append(0.0 if denom == 0.0 else 100.0 * abs(di_plus - di_minus) / denom)

    if len(dx_values) < period:
        return 0.0

    adx = float(np.mean(dx_values[:period]))
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    return float(adx)


def adx_series(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> list[float]:
    """Return an ADX series aligned one-for-one with candles."""
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    out = [0.0] * n
    if n < period * 2 + 1:
        return out

    tr = _true_range(highs, lows, closes)
    dm_plus = np.zeros(n)
    dm_minus = np.zeros(n)
    for i in range(1, n):
        up = float(highs[i]) - float(highs[i - 1])
        down = float(lows[i - 1]) - float(lows[i])
        if up > down and up > 0:
            dm_plus[i] = up
        if down > up and down > 0:
            dm_minus[i] = down

    smooth_tr = float(tr[1 : period + 1].sum())
    smooth_plus = float(dm_plus[1 : period + 1].sum())
    smooth_minus = float(dm_minus[1 : period + 1].sum())

    dx_values: list[float] = []
    adx = 0.0
    for i in range(period + 1, n):
        smooth_tr = smooth_tr - smooth_tr / period + float(tr[i])
        smooth_plus = smooth_plus - smooth_plus / period + float(dm_plus[i])
        smooth_minus = smooth_minus - smooth_minus / period + float(dm_minus[i])
        if smooth_tr == 0.0:
            dx = 0.0
        else:
            di_plus = 100.0 * smooth_plus / smooth_tr
            di_minus = 100.0 * smooth_minus / smooth_tr
            denom = di_plus + di_minus
            dx = 0.0 if denom == 0.0 else 100.0 * abs(di_plus - di_minus) / denom

        if len(dx_values) < period:
            dx_values.append(dx)
            if len(dx_values) == period:
                adx = float(np.mean(dx_values))
                out[i] = adx
            continue

        adx = (adx * (period - 1) + dx) / period
        out[i] = float(adx)
    return out


def indicators_at(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    index: int,
    *,
    rsi_period: int = 14,
    ema_period: int = 20,
    adx_period: int = 14,
) -> dict[str, float]:
    """Return scalar indicator values at one clamped bar index."""
    closes = np.asarray(closes, dtype=float)
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    if len(closes) == 0:
        return {"RSI": 50.0, "EMA": 0.0, "ADX": 0.0}

    i = max(0, min(index, len(closes) - 1))
    window_c = closes[: i + 1]
    return {
        "RSI": round(float(_rsi(window_c, rsi_period)) if len(window_c) > rsi_period else 50.0, 2),
        "EMA": round(float(ema_series(window_c, ema_period)[-1]), 4),
        "ADX": round(_adx_value(highs[: i + 1], lows[: i + 1], window_c, adx_period), 2),
    }
