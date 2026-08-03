"""CSV candle loading for offline CLI runs."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from koval.exchanges.base import OHLCV_COLUMNS


def load_csv_candles(path: Path) -> np.ndarray:
    """Read a chronological OHLCV CSV into an ``(N, 6)`` float array.

    The file must have a header row naming exactly the OHLCV columns; extra
    columns are ignored, missing ones are an error.
    """
    frame = pd.read_csv(path)
    missing = [column for column in OHLCV_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{path}: missing required columns: {', '.join(missing)}")
    candles = frame.loc[:, list(OHLCV_COLUMNS)].to_numpy(dtype=np.float64)
    timestamps = candles[:, 0]
    if np.any(np.diff(timestamps) <= 0):
        raise ValueError(f"{path}: rows must be chronological with strictly increasing timestamps")
    return candles
