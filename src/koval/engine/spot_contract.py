"""Host-owned, explicitly bound Binance cash Spot execution ports (no transport)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from koval.engine.broker import BrokerFill, BrokerOrderAck, BrokerOrderIntent, ProtectiveOrderIntent

SPOT_TESTNET = "binance_spot_testnet"
SPOT_PRODUCTION = "binance_spot_live_internal"
SPOT_TARGETS = frozenset({SPOT_TESTNET, SPOT_PRODUCTION})


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
    context: dict = field(default_factory=dict)


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
    context: dict = field(default_factory=dict)
    terminal_order_ids: tuple[str, ...] = ()
    terminal_list_ids: tuple[str, ...] = ()
    unknown_request_ids: tuple[str, ...] = ()
    base_free: str | None = None
    fees_complete: bool = False


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
    from koval.strategy.graph.execution_capabilities import graph_execution_capabilities

    identity = getattr(broker, "spot_identity", None)
    production = getattr(broker, "target", None) == SPOT_PRODUCTION
    target, environment, version = (
        (SPOT_PRODUCTION, "production", 2) if production else (SPOT_TESTNET, "testnet", 1)
    )
    label = "Spot production" if production else "Spot Testnet"
    if (
        not isinstance(identity, SpotIdentity)
        or identity.exchange != "binance"
        or identity.market != "spot"
        or identity.target != target
        or broker.target != target
        or identity.environment != environment
        or identity.leverage != 1
        or identity.contract_version != version
        or not isinstance(identity.account_id, str)
        or not identity.account_id.strip()
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
        raise ValueError(f"incompatible {label} identity")
    configured = getattr(config, "spot_identity", None)
    if (production or configured is not None) and configured != identity:
        raise ValueError(f"incompatible {label} config identity")
    if production:
        for name in ("spot_software_binding", "spot_claim_generation"):
            value = getattr(config, name, None)
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != getattr(broker, name, None)
            ):
                raise ValueError(f"incompatible {label} {name}")
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
        raise ValueError(f"{label} requires a durable on_record callback")
    error = f"{label} supports only long-only 1x market entry with fixed linked stop/target"
    if config.higher_timeframe is not None or config.requires_higher_timeframe:
        raise ValueError(error)
    if any(b["type"].startswith("dynamic_exit.") for b in graph["blocks"]):
        raise ValueError(error)
    if production and any(
        b["type"].startswith(("value.", "condition.", "state.", "recipe.", "level."))
        for b in graph["blocks"]
    ):
        raise ValueError(error)
    typed = compile_legacy(graph) if is_legacy_graph(graph) else graph
    if production and graph_execution_capabilities(typed):
        raise ValueError(error)
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


def spot_execution_capabilities() -> dict:
    """Return a fresh transport-free capability declaration, not venue acceptance."""
    return {
        "schema": "koval_spot_execution_capabilities_v2",
        "targets": {
            SPOT_TESTNET: {"environment": "testnet", "contract_version": 1},
            SPOT_PRODUCTION: {"environment": "production", "contract_version": 2},
        },
        "journal_version": "koval_runtime_journal_v1",
        "production_state_version": 2,
        "public_methods": [
            "spot_account",
            "spot_state",
            "restore_spot",
            "reconcile_spot",
            "close_spot",
            "halt_spot_entries",
            "observe_spot",
            "supervise_spot",
            "retry_spot_close",
            "finish_spot",
        ],
        "graph_constraints": [
            "one market BUY",
            "cash long-only 1x",
            "one fixed linked SELL bracket",
            "one symbol and primary timeframe",
        ],
        "fees": {"base_quote": "exact Decimal", "third_asset": "raw, unvalued; entries halted"},
        "restore": "verified complete archive and host head; always entry-inhibited",
        "unsupported": [
            "production transport",
            "factory production",
            "credentials",
            "trailing",
            "HTF",
            "position exits",
            "numeric/state nodes",
            "recipes",
            "Grid/DCA",
            "Futures",
            "margin",
            "other venues",
            "automatic residual retry",
        ],
    }
