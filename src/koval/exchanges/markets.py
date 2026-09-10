"""Canonical venue market names, shared by the adapter factory and the cache."""

from __future__ import annotations

_FUTURES_SPELLINGS = frozenset({"future", "futures", "usdm", "usd_m"})

_SUPPORTED_MARKETS: dict[str, frozenset[str]] = {
    "binance": frozenset({"spot", "future"}),
    "whitebit": frozenset({"spot", "future"}),
}
# The market each adapter served before markets were selectable. Keeping it as
# the default means an existing caller keeps its existing data source.
_DEFAULT_MARKET: dict[str, str] = {"binance": "future", "whitebit": "spot"}


def canonical_market(exchange_type: str) -> str:
    """Return ``spot`` or ``future`` for a caller's market spelling."""
    raw = str(exchange_type or "").strip().lower()
    if raw in _FUTURES_SPELLINGS:
        return "future"
    if raw == "spot":
        return "spot"
    raise ValueError(f"unsupported market: {exchange_type!r}")


__all__ = ["_DEFAULT_MARKET", "_SUPPORTED_MARKETS", "canonical_market"]
