"""Versioned paper execution profiles (MIT, independent of any plugin)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

PAPER_LEGACY_VERSION = "paper_legacy_v1"
PAPER_FIXED_VERSION = "paper_ohlcv_fixed_v1"
PAPER_REALISTIC_VERSION = "paper_ohlcv_realistic_v2"
EQUAL_TIMESTAMP_ORDER = (
    "funding_settlement",
    "mark_price_liquidation",
    "entry_fill",
    "bracket_activation",
    "stop_or_target_fill",
    "oco_sibling_cancel",
    "dynamic_replacement",
)
_COST_KEYS = ("commission_bps", "spread_bps", "slippage_bps")
_MIN_LEVERAGE, _MAX_LEVERAGE = 1.0, 125.0


@dataclass(frozen=True)
class PaperExecutionProfile:
    version: str
    commission_bps: float = 0.0
    spread_bps: float = 0.0
    slippage_bps: float = 0.0
    leverage: float = 1.0

    @property
    def is_fixed(self) -> bool:
        return self.version == PAPER_FIXED_VERSION

    @property
    def is_costed(self) -> bool:
        return self.version in {PAPER_FIXED_VERSION, PAPER_REALISTIC_VERSION}

    @property
    def delays_protection(self) -> bool:
        return self.version == PAPER_FIXED_VERSION

    @property
    def ambiguity_policy(self) -> str | None:
        return "conservative_stop_first" if self.version == PAPER_REALISTIC_VERSION else None

    @property
    def adjustment_fraction(self) -> float:
        return (self.spread_bps / 2 + self.slippage_bps) / 10_000

    def as_config(self) -> dict:
        if not self.is_costed:
            return {"version": self.version}
        resolved = {
            "version": self.version,
            "commission_bps": self.commission_bps,
            "spread_bps": self.spread_bps,
            "slippage_bps": self.slippage_bps,
            "leverage": self.leverage,
        }
        if self.version == PAPER_REALISTIC_VERSION:
            resolved.update(
                {
                    "ambiguity_policy": self.ambiguity_policy,
                    "equal_timestamp_order": list(EQUAL_TIMESTAMP_ORDER),
                }
            )
        return resolved


def _number(config: Mapping, key: str, *, low: float, high: float, inclusive_high: bool) -> float:
    value = config[key]
    message = f"paper profile {key} must be a finite number in the supported range"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    parsed = float(value)
    top_ok = parsed <= high if inclusive_high else parsed < high
    if not math.isfinite(parsed) or not (low <= parsed and top_ok):
        raise ValueError(message)
    return parsed


def resolve_paper_profile(config: Mapping | None) -> PaperExecutionProfile:
    if not config:
        return PaperExecutionProfile(PAPER_LEGACY_VERSION)
    if not isinstance(config, Mapping):
        raise ValueError("paper profile must be a mapping")
    version = config.get("version")
    if version == PAPER_LEGACY_VERSION:
        if set(config) != {"version"}:
            raise ValueError("paper_legacy_v1 accepts only version")
        return PaperExecutionProfile(PAPER_LEGACY_VERSION)
    if version not in {PAPER_FIXED_VERSION, PAPER_REALISTIC_VERSION}:
        raise ValueError(
            "paper profile version must be paper_legacy_v1, paper_ohlcv_fixed_v1, "
            "or paper_ohlcv_realistic_v2"
        )
    allowed = {"version", *_COST_KEYS, "leverage"}
    if not {"version", *_COST_KEYS} <= set(config) <= allowed:
        raise ValueError("paper_ohlcv_fixed_v1 requires commission_bps, spread_bps, slippage_bps")
    costs = {
        key: _number(config, key, low=0.0, high=10_000.0, inclusive_high=False)
        for key in _COST_KEYS
    }
    if costs["spread_bps"] / 2 + costs["slippage_bps"] >= 10_000:
        raise ValueError("paper profile half spread plus slippage must be below 10000 bps")
    leverage = (
        _number(config, "leverage", low=_MIN_LEVERAGE, high=_MAX_LEVERAGE, inclusive_high=True)
        if "leverage" in config
        else _MIN_LEVERAGE
    )
    return PaperExecutionProfile(str(version), leverage=leverage, **costs)
