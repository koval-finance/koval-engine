"""Exchange adapters package — adapter selection by name."""

from __future__ import annotations

from koval.exchanges.base import (
    OHLCV_COLUMNS,
    TIMEFRAMES,
    ExchangeAdapter,
    timeframe_ms,
)
from koval.exchanges.binance import BinanceAdapter
from koval.exchanges.capabilities import VenueCapabilities
from koval.exchanges.execution_compatibility import assert_supported_market
from koval.exchanges.markets import _DEFAULT_MARKET, _SUPPORTED_MARKETS, canonical_market
from koval.exchanges.ohlcv_cache import OhlcvCache
from koval.exchanges.whitebit import WhiteBITAdapter


def is_supported_exchange(name: str) -> bool:
    """Return True when ``name`` (case-insensitive) maps to a known adapter."""
    return name.lower() in _SUPPORTED_MARKETS


def get_exchange_adapter(name: str, *, exchange_type: str | None = None) -> ExchangeAdapter:
    """Return a fresh adapter for ``name`` and, optionally, an explicit market.

    Binance uses production data (``testnet=False``) — backtests need real
    historical candles, which the testnet does not serve. Omitting
    ``exchange_type`` keeps each adapter's historical default market so an
    existing caller keeps reading exactly the data it read before.
    """
    key = name.lower()
    if key not in _SUPPORTED_MARKETS:
        raise ValueError(f"Unknown exchange: {name}")
    market = canonical_market(exchange_type) if exchange_type else _DEFAULT_MARKET[key]
    assert_supported_market(key, market)
    if key == "binance":
        return BinanceAdapter(testnet=False, market=market)
    return WhiteBITAdapter(market=market)


__all__ = [
    "OHLCV_COLUMNS",
    "TIMEFRAMES",
    "ExchangeAdapter",
    "VenueCapabilities",
    "BinanceAdapter",
    "WhiteBITAdapter",
    "OhlcvCache",
    "timeframe_ms",
    "is_supported_exchange",
    "get_exchange_adapter",
    "canonical_market",
]
