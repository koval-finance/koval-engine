"""Exact finite-window indicators, batching windows instead of changing formulas.

Each lane has its own SMA seed and serial recurrence. NumPy runs the same
operation across independent lanes, with no reassociation, truncated history,
or continuous-history approximation. Sliding windows are views; only O(tile)
temporary vectors and O(bars) output arrays are allocated.

Inputs are normalized float64 price arrays, as injected by simulation hosts.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

_WINDOW_TILE = 4096


def _window_pairs(values: np.ndarray, period: int, window: int, *, ema: bool) -> np.ndarray:
    """Previous/current values within each window, including its own seed."""
    out = np.full((len(values), 2), np.nan)
    if period > window or len(values) < period:
        return out
    coefficient = 2.0 / (period + 1)

    def advance(current, value):
        if ema:
            return value * coefficient + current * (1.0 - coefficient)
        return (current * (period - 1) + value) / period

    # The growing prefix shares one seed, so calculate it only once.
    current = values[:period].mean()
    out[period - 1, 1] = current
    for index in range(period, min(window, len(values))):
        previous, current = current, advance(current, values[index])
        out[index] = previous, current
    if len(values) <= window:
        return out

    windows = sliding_window_view(values, window)
    for start in range(1, len(windows), _WINDOW_TILE):
        tile = windows[start : start + _WINDOW_TILE]
        current = tile[:, :period].mean(axis=1)
        previous = np.full(len(tile), np.nan)
        for offset in range(period, window):
            previous, current = current, advance(current, tile[:, offset])
        target = out[start + window - 1 : start + window - 1 + len(tile)]
        target[:, 0], target[:, 1] = previous, current
    return out


def rolling_ema(closes: np.ndarray, period: int, window: int) -> np.ndarray:
    """EMA previous/current pairs for every growing or rolling close window."""
    return _window_pairs(closes, period, window, ema=True)


def rolling_rsi(closes: np.ndarray, period: int, window: int) -> np.ndarray:
    """Wilder RSI pairs; the previous value uses this window's starting point."""
    out = np.full((len(closes), 2), 50.0)
    if len(closes) <= period or window <= period:
        return out
    deltas = np.diff(closes)
    gains = _window_pairs(np.where(deltas > 0, deltas, 0.0), period, window - 1, ema=False)
    losses = _window_pairs(np.where(deltas < 0, -deltas, 0.0), period, window - 1, ema=False)
    target = out[1:]
    valid = (losses != 0) & np.isfinite(losses)
    target[valid] = 100.0 - 100.0 / (1.0 + gains[valid] / losses[valid])
    target[(losses == 0) & (gains > 0)] = 100.0
    return out


def rolling_atr(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int, window: int
) -> np.ndarray:
    """Wilder ATR per window, excluding its first bar's unavailable previous close."""
    out = np.zeros(len(closes))
    if len(closes) <= period or window <= period:
        return out
    true_ranges = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
    )
    values = _window_pairs(true_ranges, period, window - 1, ema=False)[:, 1]
    out[1:] = np.where(np.isnan(values), 0.0, values)
    return out
