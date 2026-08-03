"""Opt-in Binance futures testnet smoke test.

Skipped by default. This file must never use production Binance URLs or real
credentials; it exists only for explicit user-run testnet verification.
"""

from __future__ import annotations

import os
import uuid

import pytest

from koval.engine.broker import BrokerOrderIntent
from koval.exchanges.binance_sandbox import BinanceSandboxBroker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SANDBOX_SMOKE") != "1"
    or not os.environ.get("BINANCE_SANDBOX_API_KEY")
    or not os.environ.get("BINANCE_SANDBOX_API_SECRET"),
    reason="set RUN_SANDBOX_SMOKE=1 and Binance futures testnet credentials to run",
)


def test_binance_sandbox_limit_order_query_cancel_reconcile():
    broker = BinanceSandboxBroker(
        api_key=os.environ["BINANCE_SANDBOX_API_KEY"],
        api_secret=os.environ["BINANCE_SANDBOX_API_SECRET"],
    )
    metadata = broker.fetch_metadata("BTCUSDT")
    client_order_id = f"kv-smoke-{uuid.uuid4().hex[:12]}"
    intent = BrokerOrderIntent(
        session_id="manual-smoke",
        intent_id="manual-limit",
        client_order_id=client_order_id,
        symbol="BTCUSDT",
        side="buy",
        order_type="limit",
        quantity=metadata.min_qty,
        price="1.00",
        target="binance_sandbox",
    )

    ack = broker.submit_entry(intent)
    fills = broker.poll_fills("manual-smoke")
    cancel_acks = broker.cancel_all("BTCUSDT", "manual-smoke")
    report = broker.reconcile("manual-smoke", [intent])

    assert ack.client_order_id == client_order_id
    assert fills == [] or fills[0].client_order_id == client_order_id
    assert cancel_acks[0].status == "accepted"
    assert report.safe_to_trade is True
