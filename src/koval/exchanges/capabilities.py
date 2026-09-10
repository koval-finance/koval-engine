"""What a venue can actually be asked for, declared per adapter.

A capability record is data, not policy: it says which markets an adapter
serves, where its data comes from, which order types the venue offers and how
each piece of execution evidence can be sourced. Everything a runtime must not
assume about a venue is stated here instead.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Provenance vocabulary shared by the evidence fields below.
EVIDENCE_CLASSES = ("historical", "current_snapshot", "unavailable")


@dataclass(frozen=True)
class VenueCapabilities:
    exchange: str
    market: str  # "spot" | "future"
    data_environment: str  # "production" | "testnet"
    sandbox_execution: bool
    entry_order_types: tuple[str, ...]
    take_profit_order_type: str  # "take_profit_market" | "unavailable"
    fee_schedule: str  # EVIDENCE_CLASSES
    funding_history: str  # EVIDENCE_CLASSES
    symbol_spec: str  # EVIDENCE_CLASSES
    symbol_format: str  # "BTCUSDT" | "BTC_USDT" | "BTC_PERP"


__all__ = ["EVIDENCE_CLASSES", "VenueCapabilities"]
