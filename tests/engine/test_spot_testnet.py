"""Offline contract tests for an explicitly injected Spot Testnet broker."""

import pytest

from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.spot_contract import SpotIdentity
from tests.engine.spot_fixtures import ScriptedSpotBroker


def spot_graph():
    return {
        "blocks": [
            {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 9, "slow": 21}},
            {"id": "ent", "type": "entry.long_only", "params": {"entry_type": "market"}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {"sl_pct": 2, "risk_reward": 2}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1}},
        ],
        "connections": [
            {"from": "sig", "to": "ent"},
            {"from": "ent", "to": "ex"},
            {"from": "ex", "to": "rsk"},
        ],
    }


class IdentityBroker(ScriptedSpotBroker):
    target = "binance_spot_testnet"
    exchange = "binance"
    market = "spot"
    symbol = "BTCUSDT"
    base_asset = "BTC"
    quote_asset = "USDT"
    spot_contract_version = 1


def test_explicit_spot_testnet_broker_is_admitted():
    LiveEngine(
        spot_graph(),
        LiveEngineConfig("BTCUSDT", "1h", 1000, exchange="binance", market="spot"),
        broker=IdentityBroker(),
        session_id="spot-contract",
        on_record=lambda record: None,
    )


IdentityBroker.spot_identity = SpotIdentity("test-account", "BTCUSDT", "BTC", "USDT")


@pytest.mark.parametrize(
    "field,value",
    [
        ("exchange", "whitebit"),
        ("market", "future"),
        ("environment", "production"),
        ("leverage", 2),
        ("symbol", "ETHUSDT"),
        ("contract_version", 2),
        ("base_asset", ""),
        ("quote_asset", "BTC"),
        ("account_id", ""),
    ],
)
def test_identity_mismatch_is_rejected(field, value):
    from dataclasses import replace

    broker = IdentityBroker()
    broker.spot_identity = replace(broker.spot_identity, **{field: value})
    with pytest.raises(ValueError, match="Spot Testnet identity"):
        LiveEngine(
            spot_graph(),
            LiveEngineConfig("BTCUSDT", "1h", 1000, exchange="binance", market="spot"),
            broker=broker,
            on_record=lambda r: None,
        )


@pytest.mark.parametrize(
    "block,params",
    [
        ("ent", {"entry_type": "limit"}),
        ("rsk", {"leverage": 2}),
    ],
)
def test_unsupported_graph_contract_is_rejected(block, params):
    graph = spot_graph()
    for item in graph["blocks"]:
        if item["id"] == block:
            item["params"].update(params)
    with pytest.raises(ValueError, match="Spot Testnet supports"):
        LiveEngine(
            graph,
            LiveEngineConfig("BTCUSDT", "1h", 1000, exchange="binance", market="spot"),
            broker=IdentityBroker(),
            on_record=lambda r: None,
        )


def test_public_engine_preflight_snapshot_and_restore_are_read_only():
    from tests.engine.spot_fixtures import ScriptedSpotBroker, report

    config = LiveEngineConfig("BTCUSDT", "1h", 1000, exchange="binance", market="spot")
    broker = ScriptedSpotBroker()
    engine = LiveEngine(
        spot_graph(), config, broker=broker, session_id="session", on_record=lambda r: None
    )
    assert engine.spot_account["quote_cash"] == "1000"
    state = engine.reconcile_spot()
    assert engine.spot_account["flat_confirmed"] is True
    restored_broker = ScriptedSpotBroker()
    restored_broker.reports = [report()]
    restored = LiveEngine(
        spot_graph(), config, broker=restored_broker, session_id="session", on_record=lambda r: None
    )
    restored.restore_spot(state)
    restored.reconcile_spot()
    assert restored.spot_account["entries_halted"] is True
    assert all(call[0] == "reconcile" for call in restored_broker.calls)


def test_distinct_fills_keep_unique_archive_and_callback_ids_after_restore():
    from koval.engine.spot_contract import SpotProtection
    from tests.engine.spot_fixtures import entry, execution, report

    config = LiveEngineConfig("BTCUSDT", "1m", 1000, exchange="binance", market="spot")
    broker = ScriptedSpotBroker()
    records, fills = [], []
    engine = LiveEngine(
        spot_graph(),
        config,
        broker=broker,
        session_id="session",
        on_record=records.append,
        on_fill=fills.append,
    )
    engine.reconcile_spot()
    engine._process_bar([0, 100, 101, 99, 100, 10])
    engine._spot.submit(entry())
    broker.events = [execution()]
    broker.reports = [report("800", "2")]
    engine.watch_orders()
    protected = SpotProtection(**engine.spot_account["protection"])
    restored_broker = ScriptedSpotBroker()
    restored = LiveEngine(
        spot_graph(),
        config,
        broker=restored_broker,
        session_id="session",
        on_record=records.append,
        on_fill=fills.append,
    )
    restored.restore_spot(engine.spot_state)
    restored_broker.reports = [
        report(
            "800",
            "2",
            protected=protected,
            orders=(protected.stop_client_order_id, protected.target_client_order_id),
        )
    ]
    restored.reconcile_spot()
    for _ in range(4):
        restored.watch_orders()
    restored_broker.events = [
        execution("sold", role="target", client_id=protected.target_client_order_id, price="110")
    ]
    restored_broker.reports = [report("1020", "0")]
    restored.watch_orders()

    archived = [r["payload"] for r in records if r["kind"] == "fill"]
    assert {fill["event_id"] for fill in archived} == {"e1", "sold"}
    assert len({fill["fill_id"] for fill in archived}) == 2
    assert [fill["fill_id"] for fill in fills] == [fill["fill_id"] for fill in archived]
    assert restored.spot_account["flat_confirmed"] is True


def test_public_engine_runs_existing_evaluator_and_accounts_external_fills():
    from decimal import ROUND_DOWN, Decimal

    import numpy as np

    from koval.engine.live_feed import StopSignal
    from tests.engine.spot_fixtures import ScriptedSpotBroker, execution, report

    broker = ScriptedSpotBroker()
    statuses, fills, trades = [], [], []
    engine = LiveEngine(
        spot_graph(),
        LiveEngineConfig("BTCUSDT", "1h", 1000, exchange="binance", market="spot"),
        broker=broker,
        session_id="session",
        on_record=lambda r: None,
        on_status=statuses.append,
        on_fill=fills.append,
        on_trade=trades.append,
    )
    engine.reconcile_spot()
    stop = StopSignal()

    class Feed:
        def bars(self, signal):
            prices = list(np.linspace(110, 95, 40)) + list(np.linspace(95, 101, 30))
            opened = False
            for index, price in enumerate(prices):
                yield np.array([index * 3600000, price, price + 1, price - 1, price, 1000])
                submitted = [c[1] for c in broker.calls if c[0] == "entry"]
                if submitted and not opened:
                    intent = submitted[-1]
                    qty = Decimal(intent.quantity).quantize(
                        Decimal("0.00000001"), rounding=ROUND_DOWN
                    )
                    price = Decimal(intent.price).quantize(Decimal("0.00000001"))
                    cost = qty * price
                    broker.events = [
                        execution(
                            client_id=intent.client_order_id,
                            quantity=str(qty),
                            price=str(price),
                        )
                    ]
                    broker.reports = [report(str(Decimal(1000) - cost), str(qty))]
                    engine.watch_orders()
                    assert Decimal(engine.spot_account["inventory_cost"]) == cost
                    assert len(fills) == 1
                    p = broker.last_report.protection
                    broker.events = [
                        execution(
                            "exit",
                            role="target",
                            client_id=p.target_client_order_id,
                            quantity=str(qty),
                            price=str(price),
                        )
                    ]
                    broker.reports = [report("1000", "0")]
                    engine.watch_orders()
                    opened = True
                    signal.set()
                    break
            assert opened, "The real EMA evaluator must submit a market buy"

    engine.run(Feed(), stop)
    assert len(trades) == 1
    assert trades[0]["pnl"] == pytest.approx(0)
    assert statuses[-1]["spot_account"]["flat_confirmed"]
    assert statuses[-1]["run_identity"]["execution_mode"] == "binance_spot_testnet"


@pytest.mark.parametrize(
    "target", ["binance_spot_live_internal", "live", "unknown", "binance_sandbox"]
)
def test_spot_injection_without_any_exchange_identity_cannot_bypass_matrix(target):
    class Broker:
        pass

    broker = Broker()
    broker.target = target
    with pytest.raises(ValueError):
        LiveEngine(
            spot_graph(),
            LiveEngineConfig("BTCUSDT", "1h", 1000, market="spot"),
            broker=broker,
            on_record=lambda r: None,
        )


def test_spot_factory_still_rejects_without_credentials_access():
    from koval.exchanges.sandbox_factory import SandboxBrokerConfig, build_broker

    class Env(dict):
        def get(self, *args):
            pytest.fail("must not read credentials")

    with pytest.raises(ValueError, match="unsupported sandbox broker mode"):
        build_broker(
            "binance_spot_testnet",
            SandboxBrokerConfig(exchange="binance", exchange_type="spot", env=Env()),
        )


@pytest.mark.parametrize(
    "kind",
    [
        "short",
        "both",
        "trailing",
        "native_trailing",
        "native_leverage",
        "disabled_tp",
        "higher_timeframe",
    ],
)
def test_unsupported_capabilities_fail_before_records_or_broker_operations(kind):
    from koval.strategy.graph.compat import compile_legacy

    graph = spot_graph()
    kwargs = {}
    if kind in {"short", "both"}:
        graph["blocks"][1]["type"] = "entry.short_only" if kind == "short" else "entry.both"
    elif kind == "trailing":
        graph["blocks"].insert(3, {"id": "trail", "type": "dynamic_exit.trailing", "params": {}})
        graph["connections"][-1:] = [{"from": "ex", "to": "trail"}, {"from": "trail", "to": "rsk"}]
    elif kind in {"native_trailing", "native_leverage", "disabled_tp"}:
        graph = compile_legacy(graph)
        params = next(b["params"] for b in graph["blocks"] if b["type"] == "exec.order_constructor")
        params.update(
            {"trail_pct": 1}
            if kind == "native_trailing"
            else {"leverage": 2}
            if kind == "native_leverage"
            else {"take_profit_mode": "disabled"}
        )
    else:
        kwargs["higher_timeframe"] = "4h"
    records = []
    with pytest.raises(ValueError):
        LiveEngine(
            graph,
            LiveEngineConfig("BTCUSDT", "1h", 1000, market="spot", exchange="binance", **kwargs),
            broker=IdentityBroker(),
            on_record=records.append,
        )
    assert records == []


def test_preflight_does_not_accept_a_broker_missing_contract_methods():
    class Incomplete:
        target = "binance_spot_testnet"
        exchange = "binance"
        market = "spot"
        spot_identity = SpotIdentity("account", "BTCUSDT", "BTC", "USDT")

    with pytest.raises(ValueError, match="Spot broker method"):
        LiveEngine(
            spot_graph(),
            LiveEngineConfig("BTCUSDT", "1h", 1000, market="spot", exchange="binance"),
            broker=Incomplete(),
            on_record=lambda r: None,
        )


def test_all_archive_failures_require_a_fresh_engine_for_recovery():
    from koval.engine.run_identity import content_sha256
    from tests.engine.test_spot_lifecycle import enter, runtime

    coordinator, broker, _ = runtime()
    p = enter(coordinator, broker)
    state = coordinator.export_state()
    state["binding"] = {"strategy_sha256": content_sha256(spot_graph()), "timeframe": "1h"}
    from tests.engine.spot_fixtures import report

    enabled = True

    def sink(record):
        if (
            enabled
            and record["kind"] == "event"
            and record["payload"]["event_type"] == "TRADE_OPENED"
        ):
            raise OSError("disk")

    broker.reports = [
        report("800", "2", protected=p, orders=(p.stop_client_order_id, p.target_client_order_id))
    ]
    engine = LiveEngine(
        spot_graph(),
        LiveEngineConfig("BTCUSDT", "1h", 1000, exchange="binance", market="spot"),
        broker=broker,
        session_id="session",
        on_record=sink,
    )
    engine.restore_spot({"execution": state, "archive_checkpoint": {}})
    with pytest.raises(OSError):
        engine.reconcile_spot()
    enabled = False
    with pytest.raises(RuntimeError, match="storage"):
        engine.reconcile_spot()
    before = list(broker.calls)
    with pytest.raises(RuntimeError, match="storage"):
        engine.close_spot()
    assert broker.calls == before


def test_explicit_falsey_broker_is_never_replaced_by_paper():
    class DisconnectedBroker(ScriptedSpotBroker):
        def __bool__(self):
            return False

    broker = DisconnectedBroker()
    engine = LiveEngine(
        spot_graph(),
        LiveEngineConfig("BTCUSDT", "1h", 1000, exchange="binance", market="spot"),
        broker=broker,
        session_id="session",
        on_record=lambda r: None,
    )
    engine.reconcile_spot()
    assert engine.spot_account["flat_confirmed"]
    assert broker.calls[0][0] == "reconcile"
