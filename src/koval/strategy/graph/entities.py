"""Typed entity contracts carried on dataflow-graph edges.

These models are the stable vocabulary for the engine.
``confidence``/``score``-like fields live ONLY on INTERPRETATION entities;
FACT/STATE carry ``strength`` (a measurement), never a probability of success.
``extra="forbid"`` makes a stray ``confidence`` on a FACT entity fail loudly.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_serializer, model_validator

Direction = Literal["bullish", "bearish", "neutral"]
Side = Literal["buy", "sell"]


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bar_index: int
    timestamp_ms: int
    source_node_id: str


ValueUnit = Literal["price_quote_per_base", "base_volume", "percent", "ratio", "dimensionless"]
ValueReadiness = Literal["ready", "warming_up", "missing", "stale", "invalid"]


class _ValueIdentityV1(Entity):
    """New scalar contracts only; legacy entity defaults remain untouched."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    unit: ValueUnit
    symbol: str
    timeframe: str | None
    as_of_ms: int | None
    available_at_ms: int | None
    readiness: ValueReadiness
    reason: str | None
    formula_version: str
    input_cursor: str | None

    @model_validator(mode="after")
    def _identity_contract(self):
        if not self.formula_version:
            raise ValueError("formula_version is required")
        if any(t is not None and t < 0 for t in (self.as_of_ms, self.available_at_ms)):
            raise ValueError("value clocks must be nonnegative")
        if (
            self.as_of_ms is not None
            and self.available_at_ms is not None
            and self.available_at_ms < self.as_of_ms
        ):
            raise ValueError("availability precedes input bar end")
        if self.readiness == "ready":
            if not self.symbol or not self.timeframe or not self.input_cursor:
                raise ValueError("ready value requires identity and cursor")
            if self.as_of_ms is None or self.available_at_ms is None or self.reason is not None:
                raise ValueError("ready value requires clocks and no unavailable reason")
        elif not self.reason:
            raise ValueError("unavailable value requires a reason")
        return self


class NumericValueV1(_ValueIdentityV1):
    """Versioned measurement: unavailable is null, never a fabricated number."""

    value: float | None

    @model_validator(mode="after")
    def _numeric_contract(self):
        if self.readiness == "ready":
            if self.value is None or not isfinite(self.value):
                raise ValueError("ready numeric value must be finite")
        elif self.value is not None:
            raise ValueError("unavailable numeric value must be null")
        return self


class ConditionV1(_ValueIdentityV1):
    """Versioned predicate; its unit identifies the compared measurement."""

    value: bool | None

    @model_validator(mode="after")
    def _condition_contract(self):
        if (self.value is not None) != (self.readiness == "ready"):
            raise ValueError("condition must be boolean exactly when ready")
        return self


class MarketEvent(Entity):
    """FACT — a momentary thing that happened on this bar."""

    kind: str
    direction: Direction | None = None
    strength: float | None = None  # measured 0..1 (e.g. displacement / ATR)
    price_levels: list[float] = []
    metadata: dict[str, Any] = {}


class MarketState(Entity):
    """STATE — what exists now; persisted across bars by its node."""

    kind: str
    status: str
    direction: Direction | None = None
    strength: float | None = None
    price_levels: list[float] = []
    since_bar: int = 0
    metadata: dict[str, Any] = {}


class PolicyDecision(Entity):
    """POLICY — what is allowed."""

    allowed: bool
    reason: str | None = None
    scope: Literal["trading", "execution"] = "trading"
    metadata: dict[str, Any] = {}


class SetupCandidate(Entity):
    """INTERPRETATION — assembled event chain, pre-score."""

    setup_id: str
    setup_type: str
    direction: Direction
    contributing_events: list[tuple[str, int]] = []  # (node_id, bar_index)
    window: tuple[int, int] = (0, 0)
    metadata: dict[str, Any] = {}


class ScoredSetup(Entity):
    """INTERPRETATION — setup plus additive scores."""

    setup_id: str
    setup_quality_score: int = 0
    context_score: int = 0
    final_score: int = 0
    qualified: bool = False
    reasoning_chain: str = ""


class TradingIntent(Entity):
    """INTERPRETATION→EXECUTION boundary — a validated decision to trade."""

    side: Side
    setup_id: str | None = None
    entry_model_hint: str | None = None
    metadata: dict[str, Any] = {}


class OrderRequest(Entity):
    """EXECUTION — concrete order, ready to route to a broker."""

    symbol: str
    side: Side
    entry_price: float
    stop_price: float
    target_price: float | None = None
    quantity: float
    order_type: str = "limit"
    metadata: dict[str, Any] = {}
    take_profit_mode: Literal["bracket", "disabled"] = "bracket"

    @model_validator(mode="after")
    def _validate_take_profit_mode(self):
        if self.take_profit_mode == "disabled" and self.target_price is not None:
            raise ValueError("take_profit_mode=disabled conflicts with target_price")
        return self

    @model_serializer(mode="wrap")
    def _omit_default_take_profit_mode(self, handler):
        data = handler(self)
        if self.take_profit_mode == "bracket":
            data.pop("take_profit_mode", None)
        return data


class PositionExitRequest(Entity):
    """EXECUTION — full market close of one identified current long.

    The host resolves the actual remaining quantity at execution. Decision
    evidence travels separately from the position's original entry evidence.
    """

    symbol: str
    position_id: int
    position_side: Literal["long"] = "long"
    quantity_fraction: Literal[1.0] = 1.0
    order_type: Literal["market"] = "market"
    reason: Literal["signal"] = "signal"
    decision_context: dict[str, Any] | None = None


class SpotRecipeRequest(Entity):
    """Versioned recipe boundary, separate from single-entry orders."""

    recipe_version: Literal[1] = 1
    kind: Literal["dca", "grid"]
    params: dict[str, Any]
    start: bool
    stop_hold: bool = False
    stop_flatten: bool = False


class SpotRecipeStatus(Entity):
    """Confirmed read-only account and controller feedback."""

    kind: Literal["dca", "grid"]
    phase: str
    cycle_id: str
    anchor: str | None = None
    stop: str | None = None
    target: str | None = None
    net_inventory: str
    free_quote: str
    reserved_quote: str
    free_base: str
    reserved_base: str
    observation_id: str
    containment_reason: str | None = None


class OrderExecution(Entity):
    """EXECUTION — fill/ack feedback feeding Account State."""

    order_id: str
    status: str
    fill_price: float | None = None
    filled_qty: float = 0.0
