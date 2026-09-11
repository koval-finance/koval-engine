"""Sandbox replacements must preserve protection until new orders are accepted."""

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from koval.engine.broker import ProtectiveOrderIntent
from koval.exchanges.binance_sandbox import BinanceSandboxBroker
from tests.exchanges.test_binance_sandbox import _exchange_info

URL = "https://testnet.binancefuture.com/fapi/v1/order"


def _broker():
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker._active_protection = ProtectiveOrderIntent(
        "s",
        "entry",
        "old-stop",
        "old-target",
        "BTCUSDT",
        "buy",
        "0.01",
        "95",
        "110",
        "binance_sandbox",
    )
    for identifier, role in [("old-stop", "stop"), ("old-target", "target")]:
        broker._track_order(identifier, "BTCUSDT", role, "buy", "s")
    return broker


@responses.activate
def test_target_and_stop_replace_as_one_validated_snapshot():
    broker = _broker()
    responses.get("https://testnet.binancefuture.com/fapi/v1/exchangeInfo", json=_exchange_info())
    responses.post(URL, json={"orderId": 1, "status": "NEW"})
    responses.post(URL, json={"orderId": 2, "status": "NEW"})
    responses.delete(URL, json={"status": "CANCELED"})
    responses.delete(URL, json={"status": "CANCELED"})
    ack = broker.modify_protection(stop_price=111, target_price=120)
    assert ack.status == "accepted"
    methods = [call.request.method for call in responses.calls]
    assert methods == ["GET", "POST", "POST", "DELETE", "DELETE"]
    assert Decimal(
        parse_qs(urlparse(responses.calls[1].request.url).query)["stopPrice"][0]
    ) == Decimal("111")
    assert Decimal(broker._active_protection.target_price) == Decimal("120")


@responses.activate
def test_second_leg_transport_failure_keeps_old_orders_tracked_for_containment():
    broker = _broker()
    original = broker._active_protection
    responses.get("https://testnet.binancefuture.com/fapi/v1/exchangeInfo", json=_exchange_info())
    responses.post(URL, json={"orderId": 1, "status": "NEW"})
    responses.post(URL, body=requests.ConnectionError("target unavailable"))
    with pytest.raises(requests.ConnectionError):
        broker.modify_protection(target_price=120)
    assert broker._active_protection == original
    assert {"old-stop", "old-target"} <= broker._tracked_orders.keys()
    assert len(broker._tracked_orders) == 4
    assert all(call.request.method != "DELETE" for call in responses.calls)


def test_invalid_bracket_never_touches_the_venue():
    broker = _broker()
    with pytest.raises(ValueError, match="tighten"):
        broker.modify_protection(stop_price=90, target_price=120)
    assert len(broker._tracked_orders) == 2


@responses.activate
def test_stop_only_update_reports_the_accepted_normalized_price():
    broker = _broker()
    responses.get("https://testnet.binancefuture.com/fapi/v1/exchangeInfo", json=_exchange_info())
    responses.post(URL, json={"orderId": 1, "status": "NEW"})
    responses.delete(URL, json={"status": "CANCELED"})
    ack = broker.modify_stop(97.003)
    assert Decimal(ack.metadata["stop_price"]) == Decimal(broker._active_protection.stop_price)
    assert Decimal(ack.metadata["stop_price"]) != Decimal("97.003")


@responses.activate
@pytest.mark.parametrize("replacement_status,cancel_status", [("FILLED", None), ("NEW", "NEW")])
def test_stop_only_uncertainty_keeps_orders_tracked(replacement_status, cancel_status):
    broker = _broker()
    responses.get("https://testnet.binancefuture.com/fapi/v1/exchangeInfo", json=_exchange_info())
    responses.post(URL, json={"orderId": 1, "status": replacement_status})
    if cancel_status is not None:
        responses.delete(URL, json={"status": cancel_status})
    with pytest.raises(RuntimeError, match="confirmed"):
        broker.modify_stop(97)
    assert {"old-stop", "old-target"} <= broker._tracked_orders.keys()
    assert len(broker._tracked_orders) == 3
