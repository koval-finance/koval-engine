"""Canonical venue, market, and execution-mode compatibility policy."""

from __future__ import annotations

from dataclasses import dataclass

from koval.exchanges.markets import _SUPPORTED_MARKETS, canonical_market


@dataclass(frozen=True)
class ExecutionCompatibility:
    exchange: str
    market: str
    execution_mode: str
    sandbox_execution: bool
    allow_long: bool
    allow_short: bool
    max_leverage: float | None


_EXECUTION_MATRIX: dict[tuple[str, str, str], ExecutionCompatibility] = {
    ("binance", "spot", "paper"): ExecutionCompatibility(
        "binance", "spot", "paper", False, True, False, 1.0
    ),
    ("binance", "future", "paper"): ExecutionCompatibility(
        "binance", "future", "paper", False, True, True, 125.0
    ),
    ("binance", "future", "binance_sandbox"): ExecutionCompatibility(
        "binance", "future", "binance_sandbox", True, True, True, 125.0
    ),
    ("whitebit", "spot", "paper"): ExecutionCompatibility(
        "whitebit", "spot", "paper", False, True, False, 1.0
    ),
    ("whitebit", "future", "paper"): ExecutionCompatibility(
        "whitebit", "future", "paper", False, True, True, None
    ),
}


def assert_supported_market(exchange: str, market: str) -> tuple[str, str]:
    venue = str(exchange).strip().lower()
    canonical = canonical_market(market)
    if venue not in _SUPPORTED_MARKETS or canonical not in _SUPPORTED_MARKETS[venue]:
        raise ValueError(f"unsupported venue/market pair: {venue}/{canonical}")
    return venue, canonical


def compatibility_for(exchange: str, market: str, execution_mode: str) -> ExecutionCompatibility:
    venue, canonical = assert_supported_market(exchange, market)
    key = (venue, canonical, str(execution_mode).strip().lower())
    try:
        return _EXECUTION_MATRIX[key]
    except KeyError as exc:
        raise ValueError(
            "incompatible execution selection: "
            f"exchange={venue}, market={canonical}, mode={execution_mode}"
        ) from exc


__all__ = [
    "ExecutionCompatibility",
    "assert_supported_market",
    "compatibility_for",
]
