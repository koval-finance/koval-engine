"""Scripted broker responses; no venue simulator or network."""

from dataclasses import replace

from koval.engine.broker import BrokerOrderAck, BrokerOrderIntent
from koval.engine.spot_contract import (
    SPOT_TESTNET,
    SpotFill,
    SpotIdentity,
    SpotProtection,
    SpotReconciliation,
)


class ScriptedSpotBroker:
    target = SPOT_TESTNET
    exchange = "binance"
    market = "spot"
    spot_identity = SpotIdentity("account", "BTCUSDT", "BTC", "USDT")

    def __init__(self):
        self.calls = []
        self.events = []
        self.reports = []
        self.protection_response = None
        self.entry_error = None
        self.exit_error = None
        self.last_report = report()

    def reconcile_spot(self, session_id, intents):
        self.calls.append(("reconcile", tuple(i.client_order_id for i in intents)))
        if self.reports:
            self.last_report = self.reports.pop(0)
        return self.last_report

    def submit_entry(self, intent):
        self.calls.append(("entry", intent))
        if self.entry_error:
            raise self.entry_error
        return ack(intent)

    def poll_fills(self, session_id):
        self.calls.append(("poll", session_id))
        events, self.events = self.events, []
        return events

    def cancel_entry(self, symbol, session_id, client_order_id):
        self.calls.append(("cancel_entry", client_order_id))

    def place_spot_protection(self, intent):
        self.calls.append(("protect", intent))
        if isinstance(self.protection_response, Exception):
            raise self.protection_response
        result = (
            self.protection_response(intent)
            if callable(self.protection_response)
            else self.protection_response or protection(intent)
        )
        self.last_report = replace(
            self.last_report,
            protection=result,
            open_order_ids=(result.stop_client_order_id, result.target_client_order_id),
        )
        return result

    def cancel_all(self, symbol, session_id):
        self.calls.append(("cancel_all", session_id))
        return []

    def submit_exit(self, intent):
        self.calls.append(("exit", intent))
        if self.exit_error:
            raise self.exit_error
        return ack(intent, status="filled")  # Deliberately not evidence of a fill.


def ack(intent, status="accepted"):
    return BrokerOrderAck(intent.session_id, intent.client_order_id, "venue", status, SPOT_TESTNET)


def entry():
    return BrokerOrderIntent(
        "session",
        "intent",
        "entry",
        "BTCUSDT",
        "buy",
        "market",
        "2",
        SPOT_TESTNET,
        price="100",
        stop_price="90",
        target_price="120",
    )


def execution(
    event="e1", quantity="2", status="filled", role="entry", client_id="entry", price="100", fees=()
):
    return SpotFill(
        "session",
        client_id,
        "venue",
        "BTCUSDT",
        "buy" if role == "entry" else "sell",
        status,
        role,
        quantity,
        price,
        1000,
        event_id=event,
        fees=fees,
    )


def report(
    cash="1000", quantity="0", *, pending=(), fills=(), protected=None, orders=(), complete=True
):
    return SpotReconciliation(
        ScriptedSpotBroker.spot_identity,
        "session",
        "revision",
        cash,
        quantity,
        tuple(fills),
        tuple(orders),
        tuple(pending),
        protected,
        complete,
    )


def protection(intent, *, quantity=None, dust="0"):
    return SpotProtection(
        "session",
        "BTCUSDT",
        "oco-list",
        intent.stop_client_order_id,
        intent.target_client_order_id,
        quantity or intent.quantity,
        intent.stop_price,
        intent.target_price,
        dust,
        "lot_size" if dust != "0" else "",
        "0.01" if dust != "0" else "0",
    )
