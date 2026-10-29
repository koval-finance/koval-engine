"""Offline conformance for explicitly bound, injected cash Spot execution."""

from dataclasses import asdict, replace

import pytest

from koval.engine import spot_contract
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.spot_contract import SpotIdentity
from tests.engine.spot_fixtures import ScriptedSpotBroker, report
from tests.engine.test_spot_testnet import spot_graph

PRODUCTION = "binance_spot_live_internal"
IDENTITY = SpotIdentity(
    "synthetic-account",
    "BTCUSDT",
    "BTC",
    "USDT",
    target=PRODUCTION,
    environment="production",
    contract_version=2,
)


class ProductionBroker(ScriptedSpotBroker):
    target = PRODUCTION
    spot_identity = IDENTITY
    spot_software_binding = "synthetic-software-sha256"
    spot_claim_generation = "synthetic-claim-1"

    def submit_entry(self, intent):
        ack = super().submit_entry(intent)
        return replace(ack, target=PRODUCTION, metadata={"spot_context": self.context})

    def submit_exit(self, intent):
        ack = super().submit_exit(intent)
        return replace(ack, target=PRODUCTION, metadata={"spot_context": self.context})

    def __init__(self):
        super().__init__()
        self.last_report = replace(report(), identity=IDENTITY)


def config(**changes):
    return LiveEngineConfig(
        "BTCUSDT",
        "1m",
        1000,
        exchange="binance",
        market="spot",
        spot_identity=IDENTITY,
        spot_software_binding="synthetic-software-sha256",
        spot_claim_generation="synthetic-claim-1",
        **changes,
    )


def test_production_surface_and_real_fixed_bracket_graph_admission():
    assert getattr(spot_contract, "SPOT_PRODUCTION", None) == PRODUCTION
    capabilities = spot_contract.spot_execution_capabilities()
    assert capabilities["schema"] == "koval_spot_execution_capabilities_v2"
    broker = ProductionBroker()
    engine = LiveEngine(
        spot_graph(), config(), broker=broker, session_id="session", on_record=lambda r: None
    )
    assert broker.calls == []
    assert engine.spot_state["execution"]["identity"] == asdict(IDENTITY)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", ""),
        ("environment", "testnet"),
        ("target", "binance_spot_testnet"),
        ("contract_version", 1),
        ("market", "future"),
        ("leverage", 2),
    ],
)
def test_crossed_production_identity_refused_before_calls(field, value):
    broker = ProductionBroker()
    broker.spot_identity = replace(IDENTITY, **{field: value})
    with pytest.raises(ValueError):
        LiveEngine(spot_graph(), config(), broker=broker, on_record=lambda r: None)
    assert broker.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("spot_identity", None),
        ("spot_identity", replace(IDENTITY, account_id="other")),
        ("spot_software_binding", None),
        ("spot_software_binding", "other"),
        ("spot_claim_generation", "other"),
    ],
)
def test_production_config_binding_required(field, value):
    broker = ProductionBroker()
    cfg = config()
    setattr(cfg, field, value)
    with pytest.raises(ValueError):
        LiveEngine(spot_graph(), cfg, broker=broker, on_record=lambda r: None)
    assert broker.calls == []


def test_production_factory_stays_unavailable():
    from koval.exchanges.sandbox_factory import SandboxBrokerConfig, build_broker

    with pytest.raises(ValueError, match="unsupported sandbox broker mode"):
        build_broker(PRODUCTION, SandboxBrokerConfig())


def live(broker=None, sink=None, host=None, capital=1000):
    records = []
    broker = broker or ProductionBroker()
    broker.last_report = replace(broker.last_report, quote_balance=str(capital))
    engine = LiveEngine(
        spot_graph(),
        replace(config(), initial_capital=capital),
        broker=broker,
        session_id="session",
        on_record=sink or records.append,
        spot_host=host,
    )
    broker.context = engine.spot_state["execution"]["context"]
    broker.last_report = replace(broker.last_report, context=broker.context)
    engine.reconcile_spot()
    return engine, broker, records


def test_halt_between_bars_is_sticky_and_does_not_close():
    engine, broker, records = live()
    engine.halt_spot_entries("operator pause")
    engine._process_bar([0, 100, 101, 99, 100, 10])
    assert engine.spot_account["entries_halted"]
    assert engine.spot_state["execution"]["halt_reason"] == "operator pause"
    assert not [c for c in broker.calls if c[0] in {"entry", "exit", "protect", "cancel_all"}]
    assert any(r["kind"] == "spot_halt" for r in records)


def test_observation_accepts_partial_unprotected_fill_without_mutating():
    from tests.engine.spot_fixtures import entry, execution

    engine, broker, _ = live()
    intent = replace(entry(), target=PRODUCTION, metadata={"spot_context": broker.context})
    engine._spot.submit(intent)
    partial = replace(
        execution(quantity="0.5", status="partial"), metadata={"spot_context": broker.context}
    )
    broker.events = [partial]
    broker.last_report = replace(
        broker.last_report,
        quote_balance="950",
        base_quantity="0.5",
        open_order_ids=("entry",),
        pending_entry_ids=("entry",),
    )
    before = len(broker.calls)
    result = engine.observe_spot()
    assert result["execution"]["pending"]
    assert engine.spot_account["base_quantity"] == "0.5"
    assert engine.spot_account["protection"] is None
    assert engine.spot_account["entries_halted"]
    assert all(c[0] in {"poll", "reconcile"} for c in broker.calls[before:])


def test_production_context_crossing_refused_before_fill_accounting():
    from tests.engine.spot_fixtures import entry, execution

    engine, broker, records = live()
    engine._spot.submit(
        replace(entry(), target=PRODUCTION, metadata={"spot_context": broker.context})
    )
    broker.events = [execution()]
    with pytest.raises(ValueError, match="context"):
        engine.observe_spot()
    assert engine.spot_account["base_quantity"] == "0"
    assert not [r for r in records if r["kind"] == "fill"]


def test_halt_write_failure_inhibits_before_next_intent():
    enabled = False

    def sink(record):
        if enabled and record["kind"] == "spot_halt":
            raise OSError("synthetic disk failure")

    engine, broker, _ = live(sink=sink)
    enabled = True
    with pytest.raises(OSError):
        engine.halt_spot_entries("pause")
    assert engine.spot_account["entries_halted"]
    with pytest.raises(RuntimeError, match="storage"):
        engine.observe_spot()
    assert not [c for c in broker.calls if c[0] == "entry"]
