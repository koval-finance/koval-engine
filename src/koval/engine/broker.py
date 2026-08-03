"""Broker port for paper and sandbox live order routing.

This module is intentionally framework-free: no FastAPI, MongoDB, requests, or
Backtrader imports belong in the broker contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

OrderSide = Literal["buy", "sell"]
OrderStatus = Literal["accepted", "rejected", "partial", "filled", "canceled", "expired"]
OrderRole = Literal["entry", "stop", "target", "flatten", "cancel", "protection"]


@dataclass(frozen=True)
class BrokerOrderIntent:
    session_id: str
    intent_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: str
    quantity: str
    target: str
    price: str | None = None
    stop_price: str | None = None
    target_price: str | None = None
    role: OrderRole = "entry"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerOrderAck:
    session_id: str
    client_order_id: str
    exchange_order_id: str | None
    status: OrderStatus
    target: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerFill:
    session_id: str
    client_order_id: str
    exchange_order_id: str | None
    symbol: str
    side: OrderSide
    status: Literal["partial", "filled", "canceled", "expired"]
    role: OrderRole
    quantity: str | None
    price: str | None
    timestamp_ms: int
    realized_pnl: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtectiveOrderIntent:
    session_id: str
    entry_client_order_id: str
    stop_client_order_id: str
    target_client_order_id: str
    symbol: str
    side: OrderSide
    quantity: str
    stop_price: str
    target_price: str
    target: str
    reduce_only: bool = True
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtectiveOrderState:
    session_id: str
    entry_client_order_id: str
    stop_client_order_id: str | None
    target_client_order_id: str | None
    status: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    symbol: str
    side: str
    quantity: str
    entry_price: str | None = None
    position_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerReconciliationReport:
    session_id: str
    target: str
    open_orders: list[BrokerOrderAck] = field(default_factory=list)
    fills: list[BrokerFill] = field(default_factory=list)
    positions: list[BrokerPositionSnapshot] = field(default_factory=list)
    incidents: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def safe_to_trade(self) -> bool:
        return not self.incidents


class BrokerPositionView(Protocol):
    """Minimum position state the engine needs from every broker."""

    side: str
    entry_price: float | str
    quantity: float | str


class Broker(Protocol):
    target: str

    @property
    def pending(self) -> bool:
        """Whether an entry order is still working."""

    @property
    def position(self) -> BrokerPositionView | None:
        """Current reconciled/open position, if any."""

    def submit_entry(self, intent: BrokerOrderIntent) -> BrokerOrderAck:
        """Submit an entry order after the caller has persisted the intent."""

    def place_protection(self, intent: ProtectiveOrderIntent) -> list[BrokerOrderAck]:
        """Place or replace protective reduce-only stop/target orders."""

    def poll_fills(self, session_id: str) -> list[BrokerFill]:
        """Poll venue order state and return new fill/terminal events."""

    def reconcile(
        self, session_id: str, intents: list[BrokerOrderIntent]
    ) -> BrokerReconciliationReport:
        """Reconcile persisted intents against venue state before trading."""

    def cancel_all(self, symbol: str, session_id: str) -> list[BrokerOrderAck]:
        """Cancel all known working orders for a symbol/session."""

    def flatten(self, symbol: str, session_id: str) -> BrokerOrderAck | None:
        """Flatten or reduce open exposure if the venue has any."""

    def modify_stop(self, stop_price: float) -> BrokerOrderAck | None:
        """Replace the active protective stop without increasing exposure."""
