"""Typed entity contracts carried on dataflow-graph edges.

These eight models are the stable vocabulary for the engine.
``confidence``/``score``-like fields live ONLY on INTERPRETATION entities;
FACT/STATE carry ``strength`` (a measurement), never a probability of success.
``extra="forbid"`` makes a stray ``confidence`` on a FACT entity fail loudly.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Direction = Literal["bullish", "bearish", "neutral"]
Side = Literal["buy", "sell"]


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bar_index: int
    timestamp_ms: int
    source_node_id: str


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


class OrderExecution(Entity):
    """EXECUTION — fill/ack feedback feeding Account State."""

    order_id: str
    status: str
    fill_price: float | None = None
    filled_qty: float = 0.0
