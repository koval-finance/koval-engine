"""Current Binance conditional API, including child-order fill reconciliation."""

from urllib.parse import parse_qs, urlparse

import pytest
import responses

from koval.engine.broker import ProtectiveOrderIntent
from koval.exchanges.binance_sandbox import BinanceSandboxBroker

BASE = "https://testnet.binancefuture.com"


def _protection():
    return ProtectiveOrderIntent(
        "s", "entry", "stop", "target", "BTCUSDT", "buy", "1", "90", "120", "binance_sandbox"
    )


@responses.activate
def test_default_protection_uses_algo_service():
    responses.post(
        BASE + "/fapi/v1/algoOrder", json={"algoId": 1, "algoStatus": "NEW", "clientAlgoId": "stop"}
    )
    responses.post(
        BASE + "/fapi/v1/algoOrder",
        json={"algoId": 2, "algoStatus": "NEW", "clientAlgoId": "target"},
    )
    broker = BinanceSandboxBroker(api_key="key", api_secret="secret")
    acks = broker.place_protection(_protection())
    assert [ack.status for ack in acks] == ["accepted", "accepted"]
    params = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert params["algoType"] == ["CONDITIONAL"]
    assert params["triggerPrice"] == ["90"]
    assert params["clientAlgoId"] == ["stop"]
    assert "newClientOrderId" not in params
    assert broker.resolved_metadata["conditional_order_api"] == "algo"
    assert broker.resolved_metadata["version"] == "binance_sandbox_v2"


@responses.activate
def test_triggered_algo_reads_actual_child_fills_once():
    from koval.exchanges.binance_algo import AlgoOrderRouter

    calls = []

    def request(method, path, params):
        calls.append((method, path, params))
        if path.endswith("algoOrder"):
            return {
                "algoStatus": "FINISHED",
                "algoId": 7,
                "clientAlgoId": "stop",
                "actualOrderId": "42",
                "symbol": "BTCUSDT",
            }
        return {
            "status": "FILLED",
            "orderId": 42,
            "clientOrderId": "child",
            "executedQty": "1",
            "avgPrice": "89",
            "symbol": "BTCUSDT",
        }

    router = AlgoOrderRouter(request)
    router.track("stop")
    result = router.request(
        "GET", "/fapi/v1/order", {"symbol": "BTCUSDT", "origClientOrderId": "stop"}
    )
    assert calls[-1] == ("GET", "/fapi/v1/order", {"symbol": "BTCUSDT", "orderId": "42"})
    assert result["clientOrderId"] == "stop"
    assert result["orderId"] == 42
    assert result["executedQty"] == "1"


def test_cancellation_is_verified_and_child_race_is_preserved():
    from koval.exchanges.binance_algo import AlgoOrderRouter

    calls = []

    def request(method, path, params):
        calls.append((method, path, params))
        if method == "DELETE" and path.endswith("algoOrder"):
            return {"code": "200", "msg": "success"}
        if path.endswith("algoOrder"):
            return {"algoStatus": "FINISHED", "actualOrderId": "42", "symbol": "BTCUSDT"}
        return {"status": "FILLED", "orderId": 42, "executedQty": "1", "avgPrice": "89"}

    router = AlgoOrderRouter(request)
    router.track("stop")
    result = router.request(
        "DELETE", "/fapi/v1/order", {"symbol": "BTCUSDT", "origClientOrderId": "stop"}
    )
    assert result["status"] == "FILLED"
    assert result["executedQty"] == "1"
    assert len(calls) == 3


def test_unknown_algo_state_fails_closed():
    from koval.exchanges.binance_algo import AlgoOrderRouter

    router = AlgoOrderRouter(lambda *args: {"algoStatus": "UNKNOWN_NEW_STATE"})
    router.track("stop")
    with pytest.raises(RuntimeError, match="algo.*status"):
        router.request("GET", "/fapi/v1/order", {"origClientOrderId": "stop", "symbol": "BTCUSDT"})


@responses.activate
def test_reconciliation_sees_orphan_algo_orders_without_saved_intents():
    responses.get(BASE + "/fapi/v1/positionSide/dual", json={"dualSidePosition": False})
    responses.get(BASE + "/fapi/v2/positionRisk", json=[])
    responses.get(BASE + "/fapi/v1/openOrders", json=[])
    responses.get(
        BASE + "/fapi/v1/openAlgoOrders",
        json=[
            {
                "algoId": 7,
                "clientAlgoId": "orphan",
                "algoStatus": "NEW",
                "symbol": "BTCUSDT",
                "orderType": "STOP_MARKET",
                "side": "SELL",
                "reduceOnly": True,
            }
        ],
    )
    broker = BinanceSandboxBroker(api_key="key", api_secret="secret")
    report = broker.reconcile("s", [])
    assert not report.safe_to_trade
    assert report.open_orders[0].client_order_id == "orphan"


@responses.activate
def test_public_polling_emits_child_fill_once_with_actual_fees():
    responses.post(
        BASE + "/fapi/v1/algoOrder", json={"algoId": 1, "algoStatus": "NEW", "clientAlgoId": "stop"}
    )
    responses.post(
        BASE + "/fapi/v1/algoOrder",
        json={"algoId": 2, "algoStatus": "NEW", "clientAlgoId": "target"},
    )
    responses.get(
        BASE + "/fapi/v1/algoOrder",
        json={"algoId": 1, "algoStatus": "FINISHED", "actualOrderId": "42", "symbol": "BTCUSDT"},
    )
    responses.get(
        BASE + "/fapi/v1/order",
        json={
            "orderId": 42,
            "status": "FILLED",
            "executedQty": "1",
            "avgPrice": "89",
            "updateTime": 60_000,
            "symbol": "BTCUSDT",
        },
    )
    responses.get(
        BASE + "/fapi/v1/userTrades",
        json=[{"commission": "0.09", "commissionAsset": "USDT", "realizedPnl": "-11"}],
    )
    responses.get(
        BASE + "/fapi/v1/algoOrder",
        json={"algoId": 2, "algoStatus": "NEW", "clientAlgoId": "target"},
    )
    broker = BinanceSandboxBroker(api_key="key", api_secret="secret")
    broker.place_protection(_protection())
    fills = broker.poll_fills("s")
    assert len(fills) == 1
    assert fills[0].client_order_id == "stop"
    assert fills[0].exchange_order_id == "42"
    assert fills[0].realized_pnl == "-11.09"
    assert broker.poll_fills("s") == []
