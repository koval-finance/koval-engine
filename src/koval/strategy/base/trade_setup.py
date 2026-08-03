from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class StructureState:
    bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    last_swing_high: float | None = None
    last_swing_low: float | None = None
    active_poi: float | None = None
    bos_confirmed: bool = False
    choch_detected: bool = False
    ltf_choch_detected: bool = False
    poi_zone_upper: float | None = None
    poi_zone_lower: float | None = None


@dataclass
class ChartAnnotation:
    type: Literal["level", "zone", "marker", "label"]
    time: int
    value: float
    color: str = "#ffffff"
    label: str = ""
    zone_top: float | None = None
    zone_bottom: float | None = None


@dataclass
class TradeSetup:
    direction: Literal["long", "short"]
    entry_price: float
    stop_loss: float
    take_profit: float | None = None
    size: float | None = None
    entry_type: Literal["market", "limit", "stop"] = "limit"
    why_entry: list[str] = field(default_factory=list)
    indicators_at_entry: dict[str, Any] = field(default_factory=dict)
    sl_calc_expr: str | None = None
    tp_calc_expr: str | None = None
    annotations: list[ChartAnnotation] = field(default_factory=list)


@dataclass(frozen=True)
class EntryConfig:
    """Output of an entry block: which directions are allowed and how to enter."""

    allow_long: bool
    allow_short: bool
    entry_type: Literal["market", "limit", "stop"] = "market"
