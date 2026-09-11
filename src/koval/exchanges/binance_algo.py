"""Conditional-order wire translation for the Binance futures sandbox.

This module has no HTTP client or origin selection. The broker supplies its
allowlisted, signed transport; uncertainty is never retried as a new order.
"""

from collections.abc import Callable
from typing import Any

_CONDITIONAL_TYPES = {
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
}
_STATUSES = {
    "NEW": "NEW",
    "TRIGGERING": "NEW",
    "TRIGGERED": "NEW",
    "FINISHED": "NEW",
    "CANCELED": "CANCELED",
    "EXPIRED": "EXPIRED",
    "REJECTED": "REJECTED",
}


class AlgoOrderRouter:
    """Keep the public client ID stable across algo and actual child orders."""

    def __init__(self, request: Callable[[str, str, dict], dict]) -> None:
        self._request = request
        self._identifiers: set[str] = set()

    def track(self, identifier: str) -> None:
        self._identifiers.add(identifier)

    def forget(self, identifier: str) -> None:
        self._identifiers.discard(identifier)

    @staticmethod
    def normalize(payload: dict[str, Any], identifier: str = "") -> dict[str, Any]:
        status = str(payload.get("algoStatus", "")).upper()
        if status not in _STATUSES:
            raise RuntimeError(f"unknown Binance algo order status: {status}")
        return {
            **payload,
            "clientOrderId": payload.get("clientAlgoId") or identifier,
            "orderId": payload.get("algoId"),
            "type": payload.get("orderType"),
            "status": _STATUSES[status],
            "executedQty": "0",
            "metadata": {"conditional_order_api": "algo", "algo_id": payload.get("algoId")},
        }

    def request(self, method: str, path: str, params: dict) -> dict:
        if path != "/fapi/v1/order":
            return self._request(method, path, params)
        if method == "POST" and params.get("type") in _CONDITIONAL_TYPES:
            translated = dict(params)
            identifier = translated.pop("newClientOrderId")
            self.track(identifier)  # Track before I/O, including uncertain submissions.
            translated["clientAlgoId"] = identifier
            translated["algoType"] = "CONDITIONAL"
            if "stopPrice" in translated:
                translated["triggerPrice"] = translated.pop("stopPrice")
            translated.pop("newOrderRespType", None)
            return self.normalize(
                self._request(method, "/fapi/v1/algoOrder", translated), identifier
            )
        identifier = str(params.get("origClientOrderId", ""))
        if identifier not in self._identifiers or method not in {"GET", "DELETE"}:
            return self._request(method, path, params)
        query = {"clientAlgoId": identifier}
        if method == "DELETE":
            result = self._request("DELETE", "/fapi/v1/algoOrder", query)
            if str(result.get("code")) != "200":
                raise RuntimeError("algo cancellation was not confirmed")
        algo = self._request("GET", "/fapi/v1/algoOrder", query)
        normalized = self.normalize(algo, identifier)
        child_id = str(algo.get("actualOrderId") or "0")
        if child_id not in {"0", "", "-1"}:
            symbol = algo.get("symbol") or params.get("symbol")
            child_params = {"symbol": symbol, "orderId": child_id}
            child = self._request("GET", "/fapi/v1/order", child_params)
            if method == "DELETE" and child.get("status") in {"NEW", "PARTIALLY_FILLED"}:
                child = self._request("DELETE", "/fapi/v1/order", child_params)
            return {
                **child,
                "symbol": symbol,
                "clientOrderId": identifier,
                "metadata": normalized["metadata"],
            }
        if algo.get("algoStatus") == "FINISHED":
            raise RuntimeError("finished algo order omitted actualOrderId")
        return normalized

    def open_orders(self) -> list[dict]:
        payload = self._request("GET", "/fapi/v1/openAlgoOrders", {})
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("invalid Binance open algo orders response")
        normalized = [self.normalize(row) for row in rows]
        for row in normalized:
            self.track(row["clientOrderId"])
        return normalized
