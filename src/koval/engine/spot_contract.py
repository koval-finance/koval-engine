"""Version-one, host-owned Binance Spot Testnet execution port (no transport)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from koval.engine.broker import BrokerFill, BrokerOrderAck, BrokerOrderIntent, ProtectiveOrderIntent

SPOT_TESTNET = "binance_spot_testnet"


@dataclass(frozen=True)
class SpotIdentity:
    account_id: str
    symbol: str
    base_asset: str
    quote_asset: str
    exchange: str = "binance"
    market: str = "spot"
    target: str = SPOT_TESTNET
    environment: str = "testnet"
    leverage: int = 1
    contract_version: int = 1


@dataclass(frozen=True)
class SpotFee:
    asset: str
    quantity: str


@dataclass(frozen=True)
class SpotFill(BrokerFill):
    """One immutable execution delta or zero-quantity terminal order event."""

    event_id: str = ""
    fees: tuple[SpotFee, ...] = ()


@dataclass(frozen=True)
class SpotProtection:
    session_id: str
    symbol: str
    order_list_id: str
    stop_client_order_id: str
    target_client_order_id: str
    quantity: str
    stop_price: str
    target_price: str
    dust_quantity: str = "0"
    dust_reason: str = ""
    quantity_step: str = "0"
    price_tick: str = "0"


@dataclass(frozen=True)
class SpotReconciliation:
    """Complete, fresh, session-attributed venue evidence after all fill deltas."""

    identity: SpotIdentity
    session_id: str
    revision: str
    quote_balance: str
    base_quantity: str
    fills: tuple[SpotFill, ...] = ()
    open_order_ids: tuple[str, ...] = ()
    pending_entry_ids: tuple[str, ...] = ()
    protection: SpotProtection | None = None
    complete: bool = False
    quote_reserved: str = "0"


class SpotBroker(Protocol):
    target: str
    exchange: str
    market: str
    spot_identity: SpotIdentity

    def submit_entry(self, intent: BrokerOrderIntent) -> BrokerOrderAck: ...

    def poll_fills(self, session_id: str) -> list[SpotFill]: ...

    def cancel_entry(self, symbol: str, session_id: str, client_order_id: str) -> None: ...

    def place_spot_protection(self, intent: ProtectiveOrderIntent) -> SpotProtection: ...

    def reconcile_spot(
        self, session_id: str, intents: list[BrokerOrderIntent]
    ) -> SpotReconciliation: ...

    def cancel_all(self, symbol: str, session_id: str) -> list[BrokerOrderAck]: ...

    def submit_exit(self, intent: BrokerOrderIntent) -> BrokerOrderAck: ...


def validate_spot_selection(broker, config, graph: dict, *, archive_enabled: bool) -> None:
    """Admission is local and side-effect free; reconciliation is a separate gate."""
    from koval.strategy.graph.compat import compile_legacy, is_legacy_graph

    identity = getattr(broker, "spot_identity", None)
    if (
        not isinstance(identity, SpotIdentity)
        or identity.exchange != "binance"
        or identity.market != "spot"
        or identity.target != SPOT_TESTNET
        or identity.environment != "testnet"
        or identity.leverage != 1
        or identity.contract_version != 1
        or not identity.account_id
        or not identity.base_asset
        or not identity.quote_asset
        or identity.base_asset == identity.quote_asset
        or identity.symbol != config.symbol
        or identity.symbol != identity.base_asset + identity.quote_asset
        or config.exchange not in {None, "binance"}
        or config.market != "spot"
        or broker.exchange != identity.exchange
        or broker.market != identity.market
    ):
        raise ValueError("incompatible Spot Testnet identity")
    for method in (
        "submit_entry",
        "poll_fills",
        "cancel_entry",
        "place_spot_protection",
        "reconcile_spot",
        "cancel_all",
        "submit_exit",
    ):
        if not callable(getattr(broker, method, None)):
            raise ValueError(f"missing Spot broker method: {method}")
    if not archive_enabled:
        raise ValueError("Spot Testnet requires a durable on_record callback")
    error = "Spot Testnet supports only long-only 1x market entry with fixed linked stop/target"
    if config.higher_timeframe is not None or config.requires_higher_timeframe:
        raise ValueError(error)
    if any(b["type"].startswith("dynamic_exit.") for b in graph["blocks"]):
        raise ValueError(error)
    typed = compile_legacy(graph) if is_legacy_graph(graph) else graph
    constructors = [b for b in typed["blocks"] if b["type"] == "exec.order_constructor"]
    gates = [b for b in typed["blocks"] if b["type"] == "interp.direction_gate"]
    if any(b["type"].startswith(("aggregator.", "scoring.")) for b in typed["blocks"]):
        raise ValueError(error)
    if len(constructors) != 1 or not gates:
        raise ValueError(error)
    for block in gates:
        params = block.get("params", {})
        if params.get("allow_short", True) or not params.get("allow_long", True):
            raise ValueError(error)
    params = constructors[0].get("params", {})
    if (
        params.get("leverage", 1) != 1
        or params.get("entry_type", "market") != "market"
        or params.get("trail_pct") is not None
        or params.get("take_profit_mode", "bracket") != "bracket"
        or any(b["type"].startswith("exec.") and b not in constructors for b in typed["blocks"])
    ):
        raise ValueError(error)
