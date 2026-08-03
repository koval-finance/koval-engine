from __future__ import annotations

from koval.engine.broker import (
    Broker,
    BrokerFill,
    BrokerOrderAck,
    BrokerOrderIntent,
    BrokerReconciliationReport,
    ProtectiveOrderIntent,
)


def test_order_intent_stores_routing_and_order_fields():
    intent = BrokerOrderIntent(
        session_id="session-1",
        intent_id="intent-1",
        client_order_id="kv-entry",
        symbol="BTCUSDT",
        side="buy",
        order_type="limit",
        quantity="0.010",
        target="binance_sandbox",
        price="100.00",
        stop_price="95.00",
        target_price="110.00",
    )

    assert intent.session_id == "session-1"
    assert intent.intent_id == "intent-1"
    assert intent.client_order_id == "kv-entry"
    assert intent.symbol == "BTCUSDT"
    assert intent.side == "buy"
    assert intent.order_type == "limit"
    assert intent.quantity == "0.010"
    assert intent.price == "100.00"
    assert intent.stop_price == "95.00"
    assert intent.target_price == "110.00"
    assert intent.target == "binance_sandbox"


def test_fill_supports_partial_and_filled_statuses():
    partial = BrokerFill(
        session_id="session-1",
        client_order_id="kv-entry",
        exchange_order_id="123",
        symbol="BTCUSDT",
        side="buy",
        status="partial",
        role="entry",
        quantity="0.005",
        price="100.00",
        timestamp_ms=1,
    )
    filled = BrokerFill(
        session_id="session-1",
        client_order_id="kv-entry",
        exchange_order_id="123",
        symbol="BTCUSDT",
        side="buy",
        status="filled",
        role="entry",
        quantity="0.010",
        price="100.00",
        timestamp_ms=2,
    )

    assert partial.status == "partial"
    assert filled.status == "filled"


def test_reconciliation_is_unsafe_when_incidents_are_present():
    report = BrokerReconciliationReport(
        session_id="session-1",
        target="binance_sandbox",
        incidents=["missing_protection"],
    )

    assert report.safe_to_trade is False


def test_fake_class_satisfies_broker_protocol():
    class FakeBroker:
        target = "fake"

        def submit_entry(self, intent: BrokerOrderIntent) -> BrokerOrderAck:
            return BrokerOrderAck(
                session_id=intent.session_id,
                client_order_id=intent.client_order_id,
                exchange_order_id="1",
                status="accepted",
                target=self.target,
            )

        def place_protection(self, intent: ProtectiveOrderIntent) -> list[BrokerOrderAck]:
            return [
                BrokerOrderAck(
                    session_id=intent.session_id,
                    client_order_id=intent.stop_client_order_id,
                    exchange_order_id="stop",
                    status="accepted",
                    target=self.target,
                )
            ]

        def poll_fills(self, session_id: str) -> list[BrokerFill]:
            return []

        def reconcile(
            self, session_id: str, intents: list[BrokerOrderIntent]
        ) -> BrokerReconciliationReport:
            return BrokerReconciliationReport(session_id=session_id, target=self.target)

        def cancel_all(self, symbol: str, session_id: str) -> list[BrokerOrderAck]:
            return []

        def flatten(self, symbol: str, session_id: str) -> BrokerOrderAck | None:
            return None

    broker: Broker = FakeBroker()

    assert broker.target == "fake"
    assert broker.poll_fills("session-1") == []
