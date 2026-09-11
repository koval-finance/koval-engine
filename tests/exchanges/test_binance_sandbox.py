from decimal import Decimal

import pytest
import requests
import responses

from koval.engine.broker import BrokerOrderIntent, ProtectiveOrderIntent
from koval.exchanges.binance_sandbox import BinanceSandboxBroker, ClockSkewExceeded


def test_constructor_defaults_to_futures_testnet_and_rejects_production_url():
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    assert broker.base_url == "https://testnet.binancefuture.com"

    try:
        BinanceSandboxBroker(
            conditional_order_api="legacy",
            api_key="key",
            api_secret="secret",
            base_url="https://fapi.binance.com",
        )
    except ValueError as exc:
        assert "testnet" in str(exc)
    else:
        raise AssertionError("expected production URL rejection")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_key": "", "api_secret": "secret"},
        {"api_key": "key", "api_secret": " "},
        {"api_key": "key", "api_secret": "secret", "timeout": 0},
        {"api_key": "key", "api_secret": "secret", "timeout": float("nan")},
        {"api_key": "key", "api_secret": "secret", "timeout": float("inf")},
    ],
)
def test_constructor_rejects_invalid_credentials_and_timeout(kwargs):
    with pytest.raises(ValueError):
        BinanceSandboxBroker(conditional_order_api="legacy", **kwargs)


@responses.activate
def test_fetch_metadata_maps_exchange_info_filters():
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json={
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "orderTypes": ["MARKET", "LIMIT", "STOP_MARKET"],
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        },
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    metadata = broker.fetch_metadata("BTCUSDT")

    assert metadata.symbol == "BTCUSDT"
    assert metadata.price_tick == "0.01"
    assert metadata.quantity_step == "0.001"
    assert "market" in metadata.allowed_order_types


@responses.activate
def test_submit_entry_sends_signed_order_without_secret_in_ack_metadata():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json={
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "orderTypes": ["MARKET"],
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "1"},
                    ],
                }
            ]
        },
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 123,
            "status": "FILLED",
            "clientOrderId": "kv-entry",
            "executedQty": "0.010",
            "avgPrice": "100.00",
            "symbol": "BTCUSDT",
        },
    )
    _no_venue_fees()
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    ack = broker.submit_entry(
        BrokerOrderIntent(
            session_id="s1",
            intent_id="i1",
            client_order_id="kv-entry",
            symbol="BTCUSDT",
            side="buy",
            order_type="market",
            quantity="0.010",
            price="100.00",
            target="binance_sandbox",
        )
    )

    request = responses.calls[2].request
    assert request.headers["X-MBX-APIKEY"] == "key"
    assert "newClientOrderId=kv-entry" in request.url
    assert "signature=" in request.url
    assert ack.status == "filled"
    assert ack.exchange_order_id == "123"
    assert "secret" not in str(ack.metadata)
    assert ack.metadata["request"]["headers"]["X-MBX-APIKEY"] == "<redacted>"


@responses.activate
def test_signed_transport_error_does_not_expose_signed_url_or_request_objects():
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"code": -1000, "msg": "failed"},
        status=500,
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key-123", api_secret="secret-123"
    )

    with pytest.raises(requests.HTTPError) as captured:
        broker._signed_request(  # noqa: SLF001
            "GET",
            "/fapi/v1/order",
            {"symbol": "BTCUSDT", "origClientOrderId": "kv-entry"},
        )

    message = str(captured.value).lower()
    assert "signature" not in message
    assert "key-123" not in message
    assert "secret-123" not in message
    assert captured.value.request is None
    assert captured.value.response is None


@responses.activate
def test_server_time_sync_measures_skew_and_corrects_signed_timestamp():
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/time",
        json={"serverTime": 11_000},
    )
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"status": "NEW", "clientOrderId": "kv-entry"},
    )
    readings = iter([10_000, 10_020, 10_020])
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy",
        api_key="key",
        api_secret="secret",
        clock_ms=lambda: next(readings),
    )

    skew = broker.synchronize_clock()
    broker._signed_request("GET", "/fapi/v1/order", {"symbol": "BTCUSDT"})  # noqa: SLF001

    assert skew == 990
    assert broker.clock_skew_ms == 990
    assert "timestamp=11010" in responses.calls[1].request.url
    assert "recvWindow=5000" in responses.calls[1].request.url


@responses.activate
def test_submit_entry_validates_metadata_before_signed_order():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json={
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "orderTypes": ["MARKET"],
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "1"},
                    ],
                }
            ]
        },
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    try:
        broker.submit_entry(
            BrokerOrderIntent(
                session_id="s1",
                intent_id="i1",
                client_order_id="kv-entry",
                symbol="BTCUSDT",
                side="buy",
                order_type="market",
                quantity="0.0005",
                price="100.00",
                target="binance_sandbox",
            )
        )
    except ValueError as exc:
        assert "invalid_quantity" in str(exc)
    else:
        raise AssertionError("expected metadata validation failure")

    assert len(responses.calls) == 2


@responses.activate
def test_submit_entry_sends_metadata_normalized_values():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=_exchange_info(["MARKET"]),
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 123,
            "status": "FILLED",
            "clientOrderId": "kv-entry",
            "executedQty": "0.010",
            "avgPrice": "100.00",
            "symbol": "BTCUSDT",
        },
    )
    _no_venue_fees()
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    broker.submit_entry(
        BrokerOrderIntent(
            session_id="s1",
            intent_id="i1",
            client_order_id="kv-entry",
            symbol="BTCUSDT",
            side="buy",
            order_type="market",
            quantity="0.01",
            price="100",
            stop_price="95",
            target_price="110",
            target="binance_sandbox",
        )
    )

    request_url = responses.calls[2].request.url
    assert "quantity=0.010" in request_url
    assert "newOrderRespType=RESULT" in request_url


@responses.activate
def test_place_protection_cancel_all_and_poll_fills_use_expected_endpoints():
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 124, "status": "NEW", "clientOrderId": "kv-stop"},
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 125, "status": "NEW", "clientOrderId": "kv-target"},
    )
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 124,
            "status": "PARTIALLY_FILLED",
            "clientOrderId": "kv-entry",
            "executedQty": "0.005",
            "avgPrice": "100.00",
            "side": "BUY",
            "symbol": "BTCUSDT",
        },
    )
    for client_order_id in ("kv-stop", "kv-target"):
        responses.add(
            responses.DELETE,
            "https://testnet.binancefuture.com/fapi/v1/order",
            json={"status": "CANCELED", "clientOrderId": client_order_id},
        )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    protection = broker.place_protection(
        ProtectiveOrderIntent(
            session_id="s1",
            entry_client_order_id="kv-entry",
            stop_client_order_id="kv-stop",
            target_client_order_id="kv-target",
            symbol="BTCUSDT",
            side="buy",
            quantity="0.010",
            stop_price="95.00",
            target_price="110.00",
            target="binance_sandbox",
        )
    )
    fills = broker.poll_fills("s1")
    cancel_acks = broker.cancel_all("BTCUSDT", "s1")

    assert [ack.client_order_id for ack in protection] == ["kv-stop", "kv-target"]
    assert "reduceOnly=true" in responses.calls[0].request.url
    assert fills[0].status == "partial"
    assert fills[0].quantity == "0.005"
    assert [ack.status for ack in cancel_acks] == ["canceled", "canceled"]


@responses.activate
def test_poll_fills_deduplicates_unchanged_order_state():
    filled_payload = {
        "orderId": 124,
        "status": "FILLED",
        "clientOrderId": "kv-entry",
        "executedQty": "0.010",
        "avgPrice": "100.00",
        "side": "BUY",
        "symbol": "BTCUSDT",
        "updateTime": 123,
    }
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json=filled_payload,
    )
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json=filled_payload,
    )
    _no_venue_fees()
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker._track_order(  # noqa: SLF001 - seed tracked order for poll test
        "kv-entry", "BTCUSDT", "entry", "buy", "s1"
    )

    first = broker.poll_fills("s1")
    second = broker.poll_fills("s1")

    assert len(first) == 1
    assert second == []


@responses.activate
def test_poll_fills_never_queries_or_relabels_another_sessions_orders():
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 123,
            "status": "FILLED",
            "clientOrderId": "session-one-entry",
            "executedQty": "0.010",
            "avgPrice": "100.00",
            "symbol": "BTCUSDT",
            "updateTime": 123,
        },
    )
    _no_venue_fees()
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker._track_order(  # noqa: SLF001
        "session-one-entry", "BTCUSDT", "entry", "buy", "session-1"
    )
    broker._track_order(  # noqa: SLF001
        "session-two-entry", "ETHUSDT", "entry", "buy", "session-2"
    )

    fills = broker.poll_fills("session-1")

    # The order query plus the venue fee lookup for the fill it discovered.
    assert len(responses.calls) == 2
    assert "session-one-entry" in responses.calls[0].request.url
    assert not any(
        "session-two-entry" in call.request.url or "ETHUSDT" in call.request.url
        for call in responses.calls
    )
    assert fills[0].session_id == "session-1"
    assert "session-two-entry" in broker._tracked_orders  # noqa: SLF001


@responses.activate
def test_reconcile_records_open_binance_position():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v2/positionRisk",
        json=[
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.010",
                "entryPrice": "100.00",
                "positionSide": "BOTH",
            }
        ],
    )
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/openOrders",
        json=[],
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    report = broker.reconcile("s1", [])

    assert report.positions[0].symbol == "BTCUSDT"
    assert report.positions[0].quantity == "0.010"
    assert report.positions[0].entry_price == "100.00"
    assert report.safe_to_trade is False
    assert "existing_position:BTCUSDT" in report.incidents


@responses.activate
def test_reconcile_does_not_adopt_even_a_protected_existing_position():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v2/positionRisk",
        json=[
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.010",
                "entryPrice": "100.00",
                "positionSide": "BOTH",
            }
        ],
    )
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/openOrders",
        json=[
            {
                "clientOrderId": "kv-stop",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "status": "NEW",
                "type": "STOP_MARKET",
                "reduceOnly": True,
            },
            {
                "clientOrderId": "kv-target",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "status": "NEW",
                "type": "TAKE_PROFIT_MARKET",
                "reduceOnly": True,
            },
        ],
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    report = broker.reconcile("s1", [])

    assert report.safe_to_trade is False
    assert report.incidents == ["existing_position:BTCUSDT"]


@responses.activate
def test_place_protection_and_cancel_all_normalize_slashed_symbol():
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 124, "status": "NEW", "clientOrderId": "kv-stop"},
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 125, "status": "NEW", "clientOrderId": "kv-target"},
    )
    for client_order_id in ("kv-stop", "kv-target"):
        responses.add(
            responses.DELETE,
            "https://testnet.binancefuture.com/fapi/v1/order",
            json={"status": "CANCELED", "clientOrderId": client_order_id},
        )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    broker.place_protection(
        ProtectiveOrderIntent(
            session_id="s1",
            entry_client_order_id="kv-entry",
            stop_client_order_id="kv-stop",
            target_client_order_id="kv-target",
            symbol="BTC/USDT",
            side="buy",
            quantity="0.010",
            stop_price="95.00",
            target_price="110.00",
            target="binance_sandbox",
        )
    )
    broker.cancel_all("BTC_USDT", "s1")

    # The venue rejects unnormalized symbols; every routed order must carry the
    # same normalized form submit_entry uses.
    assert "symbol=BTCUSDT" in responses.calls[0].request.url
    assert "symbol=BTC%2FUSDT" not in responses.calls[0].request.url
    assert "symbol=BTCUSDT" in responses.calls[1].request.url
    assert "symbol=BTCUSDT" in responses.calls[2].request.url
    assert "symbol=BTCUSDT" in responses.calls[3].request.url


@responses.activate
def test_cancel_all_only_cancels_orders_owned_by_the_requested_session():
    responses.add(
        responses.DELETE,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"status": "CANCELED", "clientOrderId": "session-one-stop"},
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker._track_order(  # noqa: SLF001
        "session-one-stop", "BTCUSDT", "stop", "buy", "session-1"
    )
    broker._track_order(  # noqa: SLF001
        "session-two-stop", "BTCUSDT", "stop", "buy", "session-2"
    )

    acks = broker.cancel_all("BTCUSDT", "session-1")

    assert [ack.client_order_id for ack in acks] == ["session-one-stop"]
    assert "origClientOrderId=session-one-stop" in responses.calls[0].request.url
    assert "session-one-stop" not in broker._tracked_orders  # noqa: SLF001
    assert "session-two-stop" in broker._tracked_orders  # noqa: SLF001


@responses.activate
def test_cancel_all_preserves_final_fill_delta_from_cancel_response():
    responses.add(
        responses.DELETE,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "status": "CANCELED",
            "clientOrderId": "session-one-entry",
            "orderId": 123,
            "symbol": "BTCUSDT",
            "executedQty": "0.010",
            "avgPrice": "100.00",
            "updateTime": 123,
        },
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker._track_order(  # noqa: SLF001
        "session-one-entry", "BTCUSDT", "entry", "buy", "session-1"
    )
    broker._cumulative_fills["session-one-entry"] = Decimal("0.005")  # noqa: SLF001

    broker.cancel_all("BTCUSDT", "session-1")
    fills = broker.poll_fills("session-1")

    assert len(fills) == 1
    assert fills[0].status == "canceled"
    assert fills[0].quantity == "0.005"
    assert broker.position is not None
    assert broker.position.quantity == "0.005"


@responses.activate
def test_flatten_places_reduce_only_market_order_for_open_position():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v2/positionRisk",
        json=[
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.010",
                "entryPrice": "100.00",
                "positionSide": "BOTH",
            }
        ],
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 999,
            "status": "FILLED",
            "clientOrderId": "kv-flatten",
            "symbol": "BTCUSDT",
            "executedQty": "0.010",
            "avgPrice": "99.00",
            "updateTime": 123,
        },
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    ack = broker.flatten("BTCUSDT", "s1")
    fills = broker.poll_fills("s1")

    assert ack is not None
    assert ack.status == "filled"
    assert len(fills) == 1
    assert fills[0].role == "flatten"
    assert broker.position is None
    request_url = responses.calls[2].request.url
    assert "side=SELL" in request_url
    assert "type=MARKET" in request_url
    assert "quantity=0.010" in request_url
    assert "reduceOnly=true" in request_url
    assert "newOrderRespType=RESULT" in request_url


def _exchange_info(order_types=None):
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "pricePrecision": 2,
                "quantityPrecision": 3,
                "orderTypes": order_types or ["MARKET", "LIMIT", "STOP_MARKET"],
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "1"},
                ],
            }
        ]
    }


def _no_venue_fees():
    """Stub the venue fee lookup every completed fill now performs."""
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/userTrades",
        json=[],
    )


def _one_way_mode():
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/positionSide/dual",
        json={"dualSidePosition": False},
    )


def _entry_intent():
    return BrokerOrderIntent(
        session_id="s1",
        intent_id="i1",
        client_order_id="kv-entry",
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        quantity="0.010",
        price="100.00",
        stop_price="95.00",
        target_price="110.00",
        target="binance_sandbox",
    )


@responses.activate
def test_submit_entry_rejects_working_entry_types_before_network():
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    for order_type in ("stop_market", "stop_limit"):
        with pytest.raises(ValueError, match="market, limit and stop"):
            broker.submit_entry(
                BrokerOrderIntent(
                    **{
                        **_entry_intent().__dict__,
                        "order_type": order_type,
                    }
                )
            )

    assert len(responses.calls) == 0


@responses.activate
def test_market_entry_uses_result_response_and_queues_immediate_fill():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=_exchange_info(["MARKET"]),
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 123,
            "status": "FILLED",
            "clientOrderId": "kv-entry",
            "executedQty": "0.010",
            "avgPrice": "100.00",
            "symbol": "BTCUSDT",
        },
    )
    _no_venue_fees()
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    ack = broker.submit_entry(_entry_intent())
    fills = broker.poll_fills("s1")

    assert ack.status == "filled"
    assert "newOrderRespType=RESULT" in responses.calls[2].request.url
    assert len(fills) == 1
    assert fills[0].status == "filled"
    assert fills[0].quantity == "0.010"
    assert broker.position is not None


@responses.activate
def test_market_entry_and_protection_are_direction_safely_normalized():
    _one_way_mode()
    metadata_payload = _exchange_info(["MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"])
    symbol = metadata_payload["symbols"][0]
    symbol["pricePrecision"] = 1
    symbol["quantityPrecision"] = 2
    symbol["filters"] = [
        {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
        {"filterType": "MIN_NOTIONAL", "notional": "1"},
    ]
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=metadata_payload,
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 123,
            "status": "FILLED",
            "clientOrderId": "kv-entry",
            "executedQty": "0.01",
            "avgPrice": "117.13",
            "symbol": "BTCUSDT",
        },
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 124, "status": "NEW", "clientOrderId": "kv-stop"},
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 125, "status": "NEW", "clientOrderId": "kv-target"},
    )
    _no_venue_fees()
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    intent = BrokerOrderIntent(
        session_id="s1",
        intent_id="i1",
        client_order_id="kv-entry",
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        quantity="0.0199",
        price="117.13",
        stop_price="114.7874",
        target_price="121.8152",
        target="binance_sandbox",
    )

    broker.submit_entry(intent)
    broker.place_protection(
        ProtectiveOrderIntent(
            session_id="s1",
            entry_client_order_id="kv-entry",
            stop_client_order_id="kv-stop",
            target_client_order_id="kv-target",
            symbol="BTCUSDT",
            side="buy",
            quantity="0.01",
            stop_price=intent.stop_price,
            target_price=intent.target_price,
            target="binance_sandbox",
        )
    )

    # Order POSTs only: a venue fee lookup now sits between the entry fill and
    # the protection orders, so select by endpoint rather than by index.
    order_urls = [
        call.request.url
        for call in responses.calls
        if call.request.method == "POST" and "/fapi/v1/order" in call.request.url
    ]
    entry_url, stop_url, target_url = order_urls[:3]
    assert "quantity=0.01" in entry_url
    assert "stopPrice=114.8" in stop_url
    assert "stopPrice=121.8" in target_url


@responses.activate
def test_hedge_mode_blocks_entry_before_order_submission():
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/positionSide/dual",
        json={"dualSidePosition": True},
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    with pytest.raises(RuntimeError, match="one-way"):
        broker.submit_entry(_entry_intent())

    assert len(responses.calls) == 1


@responses.activate
def test_poll_fills_queries_by_symbol_and_tracks_position_state():
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 123,
            "status": "FILLED",
            "clientOrderId": "kv-entry",
            "executedQty": "0.010",
            "avgPrice": "100.00",
            "side": "BUY",
            "symbol": "BTCUSDT",
            "updateTime": 123,
        },
    )
    _no_venue_fees()
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker._track_order("kv-entry", "BTCUSDT", "entry", "buy", "s1")  # noqa: SLF001
    fills = broker.poll_fills("s1")

    query_url = responses.calls[0].request.url
    assert "symbol=BTCUSDT" in query_url
    assert fills[0].role == "entry"
    assert broker.pending is False
    assert broker.position is not None
    assert broker.position.side == "buy"
    assert broker.position.quantity == "0.010"


@responses.activate
def test_poll_fills_reports_incremental_quantity_for_partial_updates():
    for status, quantity in [("PARTIALLY_FILLED", "0.005"), ("FILLED", "0.010")]:
        responses.add(
            responses.GET,
            "https://testnet.binancefuture.com/fapi/v1/order",
            json={
                "orderId": 123,
                "status": status,
                "clientOrderId": "kv-entry",
                "executedQty": quantity,
                "avgPrice": "100.00",
                "side": "BUY",
                "symbol": "BTCUSDT",
                "updateTime": 123,
            },
        )
    _no_venue_fees()
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker._track_order("kv-entry", "BTCUSDT", "entry", "buy", "s1")  # noqa: SLF001

    first = broker.poll_fills("s1")
    second = broker.poll_fills("s1")

    assert first[0].quantity == "0.005"
    assert second[0].quantity == "0.005"
    assert broker.position is not None
    assert broker.position.quantity == "0.010"


@responses.activate
def test_equal_sized_partial_fill_deltas_are_each_delivered():
    for cumulative_quantity in ("0.005", "0.010"):
        responses.add(
            responses.GET,
            "https://testnet.binancefuture.com/fapi/v1/order",
            json={
                "orderId": 123,
                "status": "PARTIALLY_FILLED",
                "clientOrderId": "kv-entry",
                "executedQty": cumulative_quantity,
                "avgPrice": "100.00",
                "side": "BUY",
                "symbol": "BTCUSDT",
                "updateTime": 123,
            },
        )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker._track_order("kv-entry", "BTCUSDT", "entry", "buy", "s1")  # noqa: SLF001

    first = broker.poll_fills("s1")
    second = broker.poll_fills("s1")

    assert first[0].quantity == "0.005"
    assert second[0].quantity == "0.005"
    assert broker.position is not None
    assert broker.position.quantity == "0.010"


@responses.activate
def test_protective_fill_preserves_position_side_and_role():
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 124, "status": "NEW", "clientOrderId": "kv-stop"},
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 125, "status": "NEW", "clientOrderId": "kv-target"},
    )
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 124,
            "status": "FILLED",
            "clientOrderId": "kv-stop",
            "executedQty": "0.010",
            "avgPrice": "95.00",
            "side": "SELL",
            "symbol": "BTCUSDT",
            "updateTime": 123,
        },
    )
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 125,
            "status": "NEW",
            "clientOrderId": "kv-target",
            "executedQty": "0",
            "side": "SELL",
            "symbol": "BTCUSDT",
        },
    )
    _no_venue_fees()
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker.place_protection(
        ProtectiveOrderIntent(
            session_id="s1",
            entry_client_order_id="kv-entry",
            stop_client_order_id="kv-stop",
            target_client_order_id="kv-target",
            symbol="BTCUSDT",
            side="buy",
            quantity="0.010",
            stop_price="95.00",
            target_price="110.00",
            target="binance_sandbox",
        )
    )

    fills = broker.poll_fills("s1")

    assert len(fills) == 1
    assert fills[0].role == "stop"
    assert fills[0].side == "buy"


@pytest.mark.parametrize("exchange_status", ["REJECTED", "EXPIRED_IN_MATCH"])
@responses.activate
def test_terminal_protection_failure_is_reported_and_untracked(exchange_status):
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 124,
            "status": exchange_status,
            "clientOrderId": "kv-stop",
            "executedQty": "0",
            "symbol": "BTCUSDT",
        },
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker._track_order("kv-stop", "BTCUSDT", "stop", "buy", "s1")  # noqa: SLF001

    fills = broker.poll_fills("s1")

    assert len(fills) == 1
    assert fills[0].status == "expired"
    assert "kv-stop" not in broker._tracked_orders  # noqa: SLF001


@responses.activate
def test_reconcile_marks_unprotected_position_unsafe():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v2/positionRisk",
        json=[
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.010",
                "entryPrice": "100.00",
                "positionSide": "BOTH",
            }
        ],
    )
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/openOrders",
        json=[],
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    report = broker.reconcile("s1", [])

    assert report.safe_to_trade is False
    assert "unprotected_position:BTCUSDT" in report.incidents


@responses.activate
def test_reconcile_reports_working_entry_without_an_open_position_as_unsafe():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v2/positionRisk",
        json=[],
    )
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/openOrders",
        json=[
            {
                "orderId": 123,
                "status": "NEW",
                "clientOrderId": "orphan-entry",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "LIMIT",
                "reduceOnly": False,
            }
        ],
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    report = broker.reconcile("s1", [])

    assert report.safe_to_trade is False
    assert len(report.open_orders) == 1
    assert "working_entry_order:BTCUSDT:orphan-entry" in report.incidents
    assert "orphan-entry" in broker._tracked_orders  # noqa: SLF001


@responses.activate
def test_rejected_exchange_status_is_not_reported_as_a_fill():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=_exchange_info(["MARKET"]),
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 123, "status": "REJECTED", "clientOrderId": "kv-entry"},
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    ack = broker.submit_entry(_entry_intent())

    assert ack.status == "rejected"
    assert broker.pending is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"side": "hold"},
        {"quantity": "0"},
        {"quantity": "NaN"},
        {"stop_price": "110.00"},
        {"target": "paper"},
        {"reduce_only": False},
    ],
)
@responses.activate
def test_place_protection_rejects_invalid_intent_before_network(overrides):
    values = {
        "session_id": "s1",
        "entry_client_order_id": "kv-entry",
        "stop_client_order_id": "kv-stop",
        "target_client_order_id": "kv-target",
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": "0.010",
        "stop_price": "95.00",
        "target_price": "110.00",
        "target": "binance_sandbox",
    }
    values.update(overrides)
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    with pytest.raises(ValueError, match="protective order"):
        broker.place_protection(ProtectiveOrderIntent(**values))


@responses.activate
def test_modify_stop_cancels_and_replaces_the_tracked_stop():
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 124, "status": "NEW", "clientOrderId": "kv-stop"},
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 125, "status": "NEW", "clientOrderId": "kv-target"},
    )
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=_exchange_info(),
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"orderId": 126, "status": "NEW"},
    )
    responses.add(
        responses.DELETE,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={"status": "CANCELED", "clientOrderId": "kv-stop"},
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker.place_protection(
        ProtectiveOrderIntent(
            session_id="s1",
            entry_client_order_id="kv-entry",
            stop_client_order_id="kv-stop",
            target_client_order_id="kv-target",
            symbol="BTCUSDT",
            side="buy",
            quantity="0.010",
            stop_price="95.00",
            target_price="110.00",
            target="binance_sandbox",
        )
    )

    ack = broker.modify_stop(97.0)

    replacement_url = responses.calls[3].request.url
    cancel_url = responses.calls[4].request.url
    assert "origClientOrderId=kv-stop" in cancel_url
    assert "stopPrice=97.00" in replacement_url
    assert ack.status == "accepted"


@responses.activate
def test_modify_stop_keeps_existing_stop_when_replacement_transport_fails():
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=_exchange_info(),
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        body=requests.ConnectionError("replacement unavailable"),
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    protection = ProtectiveOrderIntent(
        session_id="s1",
        entry_client_order_id="kv-entry",
        stop_client_order_id="kv-stop",
        target_client_order_id="kv-target",
        symbol="BTCUSDT",
        side="buy",
        quantity="0.010",
        stop_price="95.00",
        target_price="110.00",
        target="binance_sandbox",
    )
    broker._active_protection = protection  # noqa: SLF001
    broker._track_order("kv-stop", "BTCUSDT", "stop", "buy", "s1")  # noqa: SLF001

    with pytest.raises(requests.ConnectionError, match="replacement unavailable"):
        broker.modify_stop(97.0)

    assert responses.calls[1].request.method == "POST"
    assert "kv-stop" in broker._tracked_orders  # noqa: SLF001
    assert broker._active_protection == protection  # noqa: SLF001


def test_modify_stop_rejects_a_change_that_increases_risk():
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker._active_protection = ProtectiveOrderIntent(  # noqa: SLF001
        session_id="s1",
        entry_client_order_id="kv-entry",
        stop_client_order_id="kv-stop",
        target_client_order_id="kv-target",
        symbol="BTCUSDT",
        side="buy",
        quantity="0.010",
        stop_price="95.00",
        target_price="110.00",
        target="binance_sandbox",
    )

    try:
        broker.modify_stop(94.0)
    except ValueError as exc:
        assert "tighten" in str(exc)
    else:
        raise AssertionError("expected a risk-increasing stop update to fail")


def _user_trades(order_id: int, trades: list[dict]) -> None:
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/userTrades",
        json=trades,
    )


@responses.activate
def test_limit_entry_is_sent_as_gtc_limit():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=_exchange_info(["MARKET", "LIMIT", "STOP_MARKET"]),
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 7,
            "status": "NEW",
            "clientOrderId": "kv-entry",
            "symbol": "BTCUSDT",
        },
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    intent = BrokerOrderIntent(
        **{**_entry_intent().__dict__, "order_type": "limit", "price": "100.50"}
    )
    ack = broker.submit_entry(intent)

    assert ack.status == "accepted"
    sent = responses.calls[2].request.url
    assert "type=LIMIT" in sent
    assert "timeInForce=GTC" in sent
    assert "price=100.50" in sent


@responses.activate
def test_stop_entry_is_sent_as_stop_market():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=_exchange_info(["MARKET", "LIMIT", "STOP_MARKET"]),
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 11,
            "status": "NEW",
            "clientOrderId": "kv-entry",
            "symbol": "BTCUSDT",
        },
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    ack = broker.submit_entry(
        BrokerOrderIntent(**{**_entry_intent().__dict__, "order_type": "stop"})
    )

    assert ack.status == "accepted"
    sent = responses.calls[2].request.url
    assert "type=STOP_MARKET" in sent
    assert "stopPrice=100.00" in sent


@responses.activate
def test_filled_entry_reads_commission_from_user_trades():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=_exchange_info(["MARKET"]),
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 8,
            "status": "FILLED",
            "clientOrderId": "kv-entry",
            "executedQty": "0.010",
            "avgPrice": "100.20",
            "symbol": "BTCUSDT",
        },
    )
    _user_trades(
        8,
        [
            {
                "commission": "0.0004008",
                "commissionAsset": "USDT",
                "realizedPnl": "0",
                "qty": "0.010",
                "price": "100.20",
            }
        ],
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    broker.submit_entry(_entry_intent())
    (fill,) = broker.poll_fills("s1")

    assert fill.metadata["commission"] == "0.0004008"
    assert fill.metadata["commission_asset"] == "USDT"
    assert fill.metadata["commission_unconverted"] is False


@responses.activate
def test_bnb_commission_is_recorded_but_not_converted():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=_exchange_info(["MARKET"]),
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 9,
            "status": "FILLED",
            "clientOrderId": "kv-entry",
            "executedQty": "0.010",
            "avgPrice": "100.20",
            "symbol": "BTCUSDT",
        },
    )
    _user_trades(
        9,
        [
            {
                "commission": "0.0000012",
                "commissionAsset": "BNB",
                "realizedPnl": "0",
                "qty": "0.010",
                "price": "100.20",
            }
        ],
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )

    broker.submit_entry(_entry_intent())
    (fill,) = broker.poll_fills("s1")

    assert fill.metadata["commission"] == "0"
    assert fill.metadata["commission_unconverted"] is True


@responses.activate
def test_commission_asset_uses_exchange_info_quote_asset_not_a_suffix_guess():
    """A non-USDT/USDC/BUSD quote (e.g. FDUSD) must be read from exchangeInfo,
    not guessed from the symbol suffix, so its commission is attributed and
    counted rather than silently flagged unconverted."""
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json={
            "symbols": [
                {
                    "symbol": "BTCFDUSD",
                    "status": "TRADING",
                    "quoteAsset": "FDUSD",
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "orderTypes": ["MARKET"],
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "1"},
                    ],
                }
            ]
        },
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 21,
            "status": "FILLED",
            "clientOrderId": "kv-entry",
            "executedQty": "0.010",
            "avgPrice": "100.20",
            "symbol": "BTCFDUSD",
        },
    )
    _user_trades(
        21,
        [
            {
                "commission": "0.0004008",
                "commissionAsset": "FDUSD",
                "realizedPnl": "0",
                "qty": "0.010",
                "price": "100.20",
            }
        ],
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    broker.submit_entry(BrokerOrderIntent(**{**_entry_intent().__dict__, "symbol": "BTCFDUSD"}))
    (fill,) = broker.poll_fills("s1")

    assert fill.metadata["commission_asset"] == "FDUSD"
    assert fill.metadata["commission"] == "0.0004008"
    assert fill.metadata["commission_unconverted"] is False


def test_fetch_metadata_records_quote_asset():
    import responses as _responses

    with _responses.RequestsMock() as rsps:
        rsps.add(
            _responses.GET,
            "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
            json={
                "symbols": [
                    {
                        "symbol": "BTCFDUSD",
                        "status": "TRADING",
                        "quoteAsset": "FDUSD",
                        "orderTypes": ["MARKET"],
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "1"},
                        ],
                    }
                ]
            },
        )
        broker = BinanceSandboxBroker(
            conditional_order_api="legacy", api_key="key", api_secret="secret"
        )
        metadata = broker.fetch_metadata("BTCFDUSD")

    assert metadata.quote_asset == "FDUSD"


@responses.activate
def test_margin_used_reflects_position_risk_leverage_after_entry():
    _one_way_mode()
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
        json=_exchange_info(["MARKET"]),
    )
    responses.add(
        responses.POST,
        "https://testnet.binancefuture.com/fapi/v1/order",
        json={
            "orderId": 30,
            "status": "FILLED",
            "clientOrderId": "kv-entry",
            "executedQty": "0.010",
            "avgPrice": "100.20",
            "symbol": "BTCUSDT",
        },
    )
    _user_trades(30, [])
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v2/positionRisk",
        json={
            "data": [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.010",
                    "entryPrice": "100.20",
                    "leverage": "5",
                    "notional": "1.002",
                }
            ]
        },
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret"
    )
    assert broker.margin_used == 0.0

    broker.submit_entry(_entry_intent())
    broker.poll_fills("s1")

    assert broker.margin_used == pytest.approx(1.002 / 5)


@responses.activate
def test_a_host_clock_beyond_the_supported_skew_fails_closed():
    responses.add(
        responses.GET,
        "https://testnet.binancefuture.com/fapi/v1/time",
        json={"serverTime": 10_000 + 120_000},
    )
    broker = BinanceSandboxBroker(
        conditional_order_api="legacy", api_key="key", api_secret="secret", clock_ms=lambda: 10_000
    )

    with pytest.raises(ClockSkewExceeded, match="clock_skew_exceeded"):
        broker.synchronize_clock()
