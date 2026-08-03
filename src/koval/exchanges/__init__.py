"""Exchange adapters package — adapter selection by name."""

from __future__ import annotations

from koval.exchanges.base import (
    OHLCV_COLUMNS,
    TIMEFRAMES,
    ExchangeAdapter,
    timeframe_ms,
)
from koval.exchanges.binance import BinanceAdapter
from koval.exchanges.ohlcv_cache import OhlcvCache
from koval.exchanges.whitebit import WhiteBITAdapter

_SUPPORTED = {"binance", "whitebit"}


def is_supported_exchange(name: str) -> bool:
    """Return True when ``name`` (case-insensitive) maps to a known adapter."""
    return name.lower() in _SUPPORTED


def get_exchange_adapter(name: str) -> ExchangeAdapter:
    """Return a fresh adapter for ``name``. Raises ValueError on an unknown name.

    Binance uses production data (``testnet=False``) — backtests need real
    historical candles, which the testnet does not serve.
    """
    key = name.lower()
    if key == "binance":
        return BinanceAdapter(testnet=False)
    if key == "whitebit":
        return WhiteBITAdapter()
    raise ValueError(f"Unknown exchange: {name}")


__all__ = [
    "OHLCV_COLUMNS",
    "TIMEFRAMES",
    "ExchangeAdapter",
    "BinanceAdapter",
    "WhiteBITAdapter",
    "OhlcvCache",
    "timeframe_ms",
    "is_supported_exchange",
    "get_exchange_adapter",
]
