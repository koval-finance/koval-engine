"""An uncertain submission cannot be blindly repeated, even through the broker API."""

from dataclasses import replace

import pytest
import requests

from koval.engine.broker import BrokerOrderIntent
from koval.engine.venue_metadata import VenueSymbolMetadata
from koval.exchanges.binance_sandbox import BinanceSandboxBroker


def test_timeout_keeps_tracking_and_blocks_duplicate_or_new_entry(monkeypatch):
    broker = BinanceSandboxBroker(api_key="test", api_secret="test")
    monkeypatch.setattr(broker, "_ensure_one_way_mode", lambda: None)
    monkeypatch.setattr(
        broker,
        "fetch_metadata",
        lambda symbol: VenueSymbolMetadata(
            symbol="BTCUSDT",
            status="TRADING",
            price_tick="0.01",
            quantity_step="0.001",
            min_qty="0.001",
            min_notional="1",
            price_precision=2,
            quantity_precision=3,
            allowed_order_types=("market",),
        ),
    )
    sent = []

    def submit(method, path, params):
        sent.append(params.copy())
        raise requests.Timeout("accepted by venue, response lost")

    monkeypatch.setattr(broker, "_order_request", submit)
    intent = BrokerOrderIntent(
        session_id="s",
        intent_id="i",
        client_order_id="entry-1",
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        quantity="1",
        price="100",
        target="binance_sandbox",
    )
    with pytest.raises(requests.Timeout):
        broker.submit_entry(intent)
    assert broker.pending
    for candidate in (intent, replace(intent, intent_id="i2", client_order_id="entry-2")):
        with pytest.raises(RuntimeError, match="reconcil"):
            broker.submit_entry(candidate)
    assert len(sent) == 1
