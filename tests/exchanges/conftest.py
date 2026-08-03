from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest

from koval.exchanges.base import OHLCV_COLUMNS, TIMEFRAMES, ExchangeAdapter


class _FakeAdapter(ExchangeAdapter):
    def __init__(self, candles: Sequence[Sequence[float]]) -> None:
        self._all = np.asarray(candles, dtype=np.float64).reshape(-1, len(OHLCV_COLUMNS))
        self.calls: list[tuple[str, str, int, int]] = []

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> np.ndarray:
        self.calls.append((symbol, timeframe, start_ms, end_ms))
        if self._all.size == 0:
            return np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
        ts = self._all[:, 0]
        mask = (ts >= start_ms) & (ts < end_ms)
        return self._all[mask].copy()

    def metadata(self) -> dict[str, Any]:
        return {"name": "fake", "symbols": ["FAKEUSDT"], "timeframes": list(TIMEFRAMES)}


@pytest.fixture
def fake_adapter_factory():
    def _make(candles: Sequence[Sequence[float]]) -> _FakeAdapter:
        return _FakeAdapter(candles)

    return _make
