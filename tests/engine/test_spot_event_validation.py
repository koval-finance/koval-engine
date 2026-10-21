"""Reject malformed venue events before durable accounting or protection."""

from dataclasses import replace
from decimal import Decimal

import pytest

from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.spot_account import SpotLedger
from koval.engine.spot_contract import SpotFee
from tests.engine.spot_fixtures import ScriptedSpotBroker, entry, execution, report
from tests.engine.test_spot_lifecycle import runtime
from tests.engine.test_spot_testnet import spot_graph


@pytest.mark.parametrize("timestamp", [True, -1, 1000.5, 1000.0, "1000", None])
@pytest.mark.parametrize("terminal", [False, True])
def test_invalid_event_timestamp_leaves_both_ledgers_unchanged(timestamp, terminal):
    ledger = SpotLedger(ScriptedSpotBroker.spot_identity, "1000")
    fill = execution(quantity="0", price=None, status="canceled") if terminal else execution()
    before = ledger.account.snapshot()

    with pytest.raises(ValueError, match="timestamp"):
        ledger.apply(replace(fill, timestamp_ms=timestamp))

    assert ledger.cash == 1000
    assert ledger.quantity == 0
    assert ledger.events == {}
    assert ledger.account.snapshot() == before


@pytest.mark.parametrize("source", ["poll", "reconciliation"])
@pytest.mark.parametrize("terminal", [False, True])
def test_invalid_timestamp_never_reaches_durable_fill_or_protection(source, terminal):
    engine, broker, records = runtime()
    engine.submit(entry())
    fill = execution(quantity="0", price=None, status="canceled") if terminal else execution()
    fill = replace(fill, timestamp_ms=1000.5)
    broker.events = [fill] if source == "poll" else []
    broker.reports = [
        report(
            "1000" if terminal else "800",
            "0" if terminal else "2",
            fills=(fill,) if source == "reconciliation" else (),
        )
    ]

    with pytest.raises(ValueError, match="timestamp"):
        engine.poll()

    assert engine.ledger.quantity == 0
    assert not any(kind == "fill" for kind, _ in records)
    assert not any(kind in {"protect", "exit"} for kind, _ in broker.calls)
    assert engine.halted and engine.uncertain
    assert not engine.persistence_failed


@pytest.mark.parametrize(
    "changes",
    [
        {"side": "sell"},
        {"side": None},
        {"role": None, "client_order_id": "unknown"},
        {"fees": (SpotFee("USDT", "0"),)},
    ],
)
def test_zero_terminal_event_requires_valid_attribution_side_and_no_fees(changes):
    engine, broker, records = runtime()
    engine.submit(entry())
    broker.events = [replace(execution(quantity="0", price=None, status="canceled"), **changes)]
    broker.reports = [report()]

    with pytest.raises(ValueError):
        engine.poll()

    assert engine.ledger.events == {}
    assert not any(kind == "fill" for kind, _ in records)
    assert engine.halted and engine.uncertain
    assert not engine.persistence_failed


@pytest.mark.parametrize("quantity", [None, "0"])
@pytest.mark.parametrize("terminal_price", [None, "0", "999"])
@pytest.mark.parametrize("acquired", [False, True])
def test_terminal_price_never_replaces_last_execution_mark(quantity, terminal_price, acquired):
    broker = ScriptedSpotBroker()
    engine = LiveEngine(
        spot_graph(),
        LiveEngineConfig("BTCUSDT", "1h", 1000, exchange="binance", market="spot"),
        broker=broker,
        session_id="session",
        on_record=lambda record: None,
    )
    engine.reconcile_spot()
    engine._spot.submit(entry())
    broker.events = ([execution("bought", quantity="1", status="canceled")] if acquired else []) + [
        execution("terminal", quantity=quantity, price=terminal_price, status="canceled")
    ]
    broker.reports = [report("900", "1") if acquired else report()]

    engine.watch_orders()

    assert Decimal(engine.spot_account["quote_cash"]) == (900 if acquired else 1000)
    assert Decimal(engine.spot_account["base_quantity"]) == (1 if acquired else 0)
    assert Decimal(engine.spot_account["equity"]) == 1000
