from __future__ import annotations

import numpy as np


def _ema(closes: np.ndarray, period: int) -> np.ndarray:
    """Calculate exponential moving average.

    Args:
        closes: Array of closing prices.
        period: EMA period.

    Returns:
        Array of EMA values with same length as input. Early values are NaN if insufficient data.
    """
    result = np.full(len(closes), np.nan)
    if len(closes) < period:
        return result
    k = 2.0 / (period + 1)
    result[period - 1] = closes[:period].mean()
    for i in range(period, len(closes)):
        result[i] = closes[i] * k + result[i - 1] * (1.0 - k)
    return result


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    """Calculate Relative Strength Index (last value only, Wilder smoothing).

    Args:
        closes: Array of closing prices.
        period: RSI period (default 14).

    Returns:
        RSI value (0-100), or 50.0 if insufficient data.
    """
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    if len(gains) < period:
        return 50.0
    # Seed with SMA of first period
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    # Wilder smoothing over remaining bars
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    return float(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))


def _true_range(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> np.ndarray:
    """Calculate True Range for each bar.

    Args:
        highs: Array of high prices.
        lows: Array of low prices.
        closes: Array of closing prices.

    Returns:
        Array of true range values.
    """
    assert len(highs) == len(lows) == len(closes), "highs, lows, closes must have equal length"
    n = len(closes)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    return tr


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    """Calculate Average True Range (last value only, Wilder smoothing).

    Args:
        highs: Array of high prices.
        lows: Array of low prices.
        closes: Array of closing prices.
        period: ATR period (default 14).

    Returns:
        ATR value, or 0.0 if insufficient data.
    """
    if len(closes) < period + 1:
        return 0.0
    tr = _true_range(highs, lows, closes)
    atr = float(
        tr[1 : period + 1].mean()
    )  # seed: SMA of first period TR values (skip bar 0 which has no prev close)
    for i in range(period + 1, len(tr)):
        atr = (atr * (period - 1) + float(tr[i])) / period
    return atr


def _macd(
    closes: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float, float, float]:
    """Calculate MACD (last values only).

    Args:
        closes: Array of closing prices.
        fast: Fast EMA period (default 12).
        slow: Slow EMA period (default 26).
        signal: Signal EMA period (default 9).

    Returns:
        Tuple of (macd_line, signal_line, histogram), or (0.0, 0.0, 0.0) if insufficient data.
    """
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    macd_line = fast_ema - slow_ema
    valid = macd_line[~np.isnan(macd_line)]
    if len(valid) < signal:
        return 0.0, 0.0, 0.0
    sig_ema = _ema(valid, signal)
    macd_val = float(valid[-1])
    sig_val = float(sig_ema[-1]) if not np.isnan(sig_ema[-1]) else 0.0
    return macd_val, sig_val, macd_val - sig_val
