"""Binance USD-M futures testnet broker adapter."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import requests

from koval.engine.broker import (
    BrokerFill,
    BrokerOrderAck,
    BrokerOrderIntent,
    BrokerPositionSnapshot,
    BrokerReconciliationReport,
    OrderRole,
    OrderSide,
    ProtectiveOrderIntent,
)
from koval.engine.client_order_id import make_client_order_id
from koval.engine.protection import validate_protection_update
from koval.engine.venue_metadata import VenueSymbolMetadata, validate_order_against_metadata
from koval.exchanges.auth import (
    hmac_sha256,
    redact_mapping,
    safe_exception_message,
)
from koval.exchanges.binance_algo import AlgoOrderRouter

_TESTNET_URL = "https://testnet.binancefuture.com"
_ALLOWED_BASE_URLS = {_TESTNET_URL}
_SUPPORTED_ENTRY_TYPES = frozenset({"market", "limit", "stop"})
_SIGNED_RECV_WINDOW_MS = 5_000
_MAX_CLOCK_SKEW_MS = 60_000


class ClockSkewExceeded(RuntimeError):
    """The host clock is too far from venue time to sign a request safely."""


def _clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class _TrackedOrder:
    symbol: str
    role: OrderRole
    position_side: OrderSide
    session_id: str = ""


class BinanceSandboxBroker:
    target = "binance_sandbox"
    exchange = "binance"
    market = "future"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = _TESTNET_URL,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        clock_ms: Callable[[], int] = _clock_ms,
        conditional_order_api: str = "algo",
    ) -> None:
        if base_url.rstrip("/") not in _ALLOWED_BASE_URLS:
            raise ValueError("Binance sandbox broker only accepts allowlisted futures testnet URLs")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Binance sandbox api_key must be non-empty")
        if not isinstance(api_secret, str) or not api_secret.strip():
            raise ValueError("Binance sandbox api_secret must be non-empty")
        if isinstance(timeout, bool):
            raise ValueError("Binance sandbox timeout must be positive and finite")
        try:
            parsed_timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("Binance sandbox timeout must be positive and finite") from exc
        if not math.isfinite(parsed_timeout) or parsed_timeout <= 0:
            raise ValueError("Binance sandbox timeout must be positive and finite")
        if not callable(clock_ms):
            raise ValueError("Binance sandbox clock_ms must be callable")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._session = session or requests.Session()
        self._timeout = parsed_timeout
        self._clock_ms = clock_ms
        self._clock_skew_ms = 0
        self._clock_synced_at_ms: int | None = None
        self._tracked_orders: dict[str, _TrackedOrder] = {}
        self._cumulative_fills: dict[str, Decimal] = {}
        self._queued_fills: list[BrokerFill] = []
        self._metadata_by_symbol: dict[str, VenueSymbolMetadata] = {}
        self._position: BrokerPositionSnapshot | None = None
        self._active_protection: ProtectiveOrderIntent | None = None
        self._stop_revision = 0
        self._one_way_mode_verified = False
        if conditional_order_api not in {"algo", "legacy"}:
            raise ValueError("conditional_order_api must be algo or legacy")
        self._conditional_order_api = conditional_order_api
        self._algo_router = AlgoOrderRouter(self._signed_request)

    def _order_request(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._conditional_order_api == "algo":
            return self._algo_router.request(method, path, params)
        return self._signed_request(method, path, params)

    @property
    def resolved_metadata(self) -> dict[str, object]:
        return {
            "version": "binance_sandbox_v2",
            "conditional_order_api": self._conditional_order_api,
            "execution_source": "venue_reported",
            "exchange": self.exchange,
            "market": self.market,
            "protection_replacement": "accept_new_pair_before_canceling_old_pair",
        }

    @property
    def pending(self) -> bool:
        return any(order.role == "entry" for order in self._tracked_orders.values())

    @property
    def position(self) -> BrokerPositionSnapshot | None:
        return self._position

    @property
    def margin_used(self) -> float:
        """Initial margin the venue holds against the open position.

        Derived from the leverage and notional reported by
        ``/fapi/v2/positionRisk``. It is ``0.0`` until that data has been
        synced (``submit_entry`` refreshes it right after an entry fills, and
        ``reconcile`` on every cycle)."""
        position = self._position
        if position is None:
            return 0.0
        meta = position.metadata
        try:
            leverage = float(meta.get("leverage") or 0.0)  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return 0.0
        if leverage <= 0:
            return 0.0
        notional = meta.get("notional")  # type: ignore[union-attr]
        try:
            notional_value = abs(float(notional)) if notional is not None else 0.0
        except (TypeError, ValueError):
            notional_value = 0.0
        if notional_value == 0.0:
            qty = _decimal_or_zero(position.quantity)
            price = _decimal_or_zero(position.entry_price)
            notional_value = float(abs(qty * price))
        return notional_value / leverage if notional_value > 0 else 0.0

    @property
    def clock_skew_ms(self) -> int:
        return self._clock_skew_ms

    def synchronize_clock(self) -> int:
        """Measure testnet clock skew using the request midpoint."""
        started_ms = int(self._clock_ms())
        response = self._request("GET", f"{self.base_url}/fapi/v1/time")
        response.raise_for_status()
        finished_ms = int(self._clock_ms())
        payload = response.json()
        try:
            server_ms = int(payload["serverTime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Binance sandbox server time response is invalid") from exc
        midpoint_ms = started_ms + (finished_ms - started_ms) // 2
        skew_ms = server_ms - midpoint_ms
        if abs(skew_ms) > _MAX_CLOCK_SKEW_MS:
            raise ClockSkewExceeded("clock_skew_exceeded")
        self._clock_skew_ms = skew_ms
        self._clock_synced_at_ms = finished_ms
        return skew_ms

    def _refresh_position_margin(self, symbol: str) -> None:
        """Pull leverage/notional for the just-opened position so
        ``margin_used`` is real for the position's whole life rather than zero
        until the next reconcile. A failure here must not un-confirm an entry
        that already filled."""
        if self._position is None:
            return
        try:
            positions = self._fetch_positions(symbol)
        except (requests.RequestException, ValueError, KeyError):
            return
        want = self._normalize_symbol(symbol)
        match = next(
            (p for p in positions if self._normalize_symbol(p.symbol) == want),
            None,
        )
        if match is not None and self._position is not None:
            self._position = replace(
                self._position,
                metadata={**self._position.metadata, **match.metadata},
            )

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """Venue symbol form Binance accepts. Single source of truth so entry,
        protection, cancel and position lookups all route the same string."""
        return symbol.replace("/", "").replace("_", "").upper()

    def fetch_metadata(self, symbol: str) -> VenueSymbolMetadata:
        response = self._request("GET", f"{self.base_url}/fapi/v1/exchangeInfo")
        response.raise_for_status()
        normalized = self._normalize_symbol(symbol)
        for item in response.json().get("symbols", []):
            if item.get("symbol") != normalized:
                continue
            filters = {f["filterType"]: f for f in item.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            notional_filter = filters.get("MIN_NOTIONAL", {})
            market_lot_filter = filters.get("MARKET_LOT_SIZE", {})
            metadata = VenueSymbolMetadata(
                symbol=normalized,
                status=item.get("status", ""),
                price_tick=price_filter.get("tickSize", "0"),
                quantity_step=lot_filter.get("stepSize", "0"),
                min_qty=lot_filter.get("minQty", "0"),
                min_notional=notional_filter.get("notional")
                or notional_filter.get("minNotional", "0"),
                allowed_order_types=tuple(
                    str(order_type).lower() for order_type in item.get("orderTypes", [])
                ),
                price_precision=item.get("pricePrecision"),
                quantity_precision=item.get("quantityPrecision"),
                market_quantity_step=market_lot_filter.get("stepSize"),
                market_min_qty=market_lot_filter.get("minQty"),
                quote_asset=str(item.get("quoteAsset", "")).upper(),
            )
            self._metadata_by_symbol[normalized] = metadata
            return metadata
        raise ValueError(f"symbol metadata not found: {symbol}")

    def submit_entry(self, intent: BrokerOrderIntent) -> BrokerOrderAck:
        if self._tracked_orders or self._position is not None:
            raise RuntimeError("sandbox entry requires reconciliation of existing exposure/orders")
        if intent.target != self.target:
            raise ValueError("entry order target does not match the Binance sandbox")
        if intent.order_type not in _SUPPORTED_ENTRY_TYPES:
            raise ValueError(
                "Binance sandbox supports market, limit and stop entries; "
                "a working entry is protected by the between-bar order watch"
            )
        self._ensure_one_way_mode()
        venue_order_type = _order_type(intent.order_type)
        metadata = self.fetch_metadata(intent.symbol)
        normalized_intent = _normalize_entry_intent(intent, metadata)
        validation = validate_order_against_metadata(
            replace(normalized_intent, order_type=venue_order_type.lower()),
            metadata,
        )
        if not validation.ok:
            raise ValueError(f"metadata validation failed: {validation.reason}")
        params: dict[str, Any] = {
            "symbol": self._normalize_symbol(intent.symbol),
            "side": intent.side.upper(),
            "type": venue_order_type,
            "quantity": validation.normalized_quantity,
            "newClientOrderId": intent.client_order_id,
            "newOrderRespType": "RESULT",
        }
        if validation.normalized_price and params["type"] == "LIMIT":
            params["price"] = validation.normalized_price
            params["timeInForce"] = "GTC"
        if validation.normalized_price and params["type"] in {"STOP", "STOP_MARKET"}:
            params["stopPrice"] = validation.normalized_price
        if validation.normalized_price and params["type"] == "STOP":
            params["price"] = validation.normalized_price
            params["timeInForce"] = "GTC"
        self._track_order(
            intent.client_order_id,
            intent.symbol,
            "entry",
            intent.side,
            intent.session_id,
        )
        ack = self._order_request("POST", "/fapi/v1/order", params)
        order_ack = _ack_from_binance(intent.session_id, intent.client_order_id, ack, self.target)
        if order_ack.status == "filled":
            tracked = self._tracked_orders[intent.client_order_id]
            quantity = _decimal_or_zero(ack.get("executedQty"))
            fill = _fill_from_binance(intent.session_id, ack, tracked, quantity)
            if (
                fill is None
                or _decimal_or_zero(fill.quantity) <= 0
                or _decimal_or_zero(fill.price) <= 0
            ):
                raise RuntimeError(
                    "filled market response omitted a positive quantity or average price"
                )
            fill = self._annotate_venue_fees(fill)
            self._apply_fill(fill)
            self._refresh_position_margin(intent.symbol)
            self._queued_fills.append(fill)
            self._forget_order(intent.client_order_id)
        elif order_ack.status != "accepted":
            self._forget_order(intent.client_order_id)
        return order_ack

    def _ensure_one_way_mode(self) -> None:
        if self._one_way_mode_verified:
            return
        payload = self._signed_request("GET", "/fapi/v1/positionSide/dual", {})
        value = payload.get("dualSidePosition")
        if value is True or str(value).lower() == "true":
            raise RuntimeError(
                "Binance sandbox requires one-way position mode; hedge mode is unsupported"
            )
        if value is not False and str(value).lower() != "false":
            raise RuntimeError("Binance sandbox could not verify one-way position mode")
        self._one_way_mode_verified = True

    def place_protection(self, intent: ProtectiveOrderIntent) -> list[BrokerOrderAck]:
        _validate_protective_order(intent, target=self.target)
        metadata = self._metadata_by_symbol.get(self._normalize_symbol(intent.symbol))
        if metadata is not None:
            intent = _normalize_protective_order(intent, metadata)
            _validate_protective_order(intent, target=self.target)
        close_side = "SELL" if intent.side == "buy" else "BUY"
        symbol = self._normalize_symbol(intent.symbol)
        orders = [
            {
                "symbol": symbol,
                "side": close_side,
                "type": "STOP_MARKET",
                "stopPrice": intent.stop_price,
                "quantity": intent.quantity,
                "reduceOnly": "true",
                "newClientOrderId": intent.stop_client_order_id,
            },
            {
                "symbol": symbol,
                "side": close_side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": intent.target_price,
                "quantity": intent.quantity,
                "reduceOnly": "true",
                "newClientOrderId": intent.target_client_order_id,
            },
        ]
        acks = []
        for order, role in zip(orders, ("stop", "target"), strict=True):
            self._track_order(
                order["newClientOrderId"],
                symbol,
                role,
                intent.side,
                intent.session_id,
            )
            payload = self._order_request("POST", "/fapi/v1/order", order)
            ack = _ack_from_binance(
                intent.session_id, order["newClientOrderId"], payload, self.target
            )
            if ack.status not in {"accepted", "filled"}:
                self._forget_order(order["newClientOrderId"])
            acks.append(ack)
            if role == "stop" and ack.status not in {"accepted", "filled"}:
                break
        if acks and all(ack.status in {"accepted", "filled"} for ack in acks):
            self._active_protection = intent
        return acks

    def modify_stop(self, stop_price: float) -> BrokerOrderAck:
        protection = self._active_protection
        if protection is None:
            raise RuntimeError("no active protection to modify")
        try:
            new_stop = Decimal(str(stop_price))
            current_stop = Decimal(protection.stop_price)
            target_price = Decimal(protection.target_price)
        except InvalidOperation as exc:
            raise ValueError("stop update must be positive and finite") from exc
        if (
            not new_stop.is_finite()
            or not current_stop.is_finite()
            or not target_price.is_finite()
            or new_stop <= 0
            or current_stop <= 0
            or target_price <= 0
        ):
            raise ValueError("stop update must be positive and finite")
        if (protection.side == "buy" and new_stop < current_stop) or (
            protection.side == "sell" and new_stop > current_stop
        ):
            raise ValueError("stop update must tighten protection, not increase risk")
        if (protection.side == "buy" and new_stop >= target_price) or (
            protection.side == "sell" and new_stop <= target_price
        ):
            raise ValueError("stop update must remain inside the protective target")
        metadata = self.fetch_metadata(protection.symbol)
        replacement = _normalize_protective_order(
            replace(protection, stop_price=str(stop_price)), metadata
        )
        validation = validate_order_against_metadata(
            BrokerOrderIntent(
                session_id=protection.session_id,
                intent_id=f"protect-{self._stop_revision + 1}",
                client_order_id=protection.stop_client_order_id,
                symbol=protection.symbol,
                side=protection.side,
                order_type="market",
                quantity=protection.quantity,
                price=protection.target_price,
                stop_price=replacement.stop_price,
                target_price=protection.target_price,
                target=self.target,
                role="stop",
            ),
            metadata,
        )
        if not validation.ok or validation.normalized_stop_price is None:
            raise ValueError(f"metadata validation failed: {validation.reason}")
        validate_protection_update(
            side=protection.side,
            current_stop=protection.stop_price,
            stop_price=validation.normalized_stop_price,
            target_price=protection.target_price,
        )
        self._stop_revision += 1
        client_order_id = make_client_order_id(
            protection.session_id,
            f"protect-stop-{self._stop_revision}",
            self.target,
            "stop",
        )
        self._track_order(
            client_order_id,
            protection.symbol,
            "stop",
            protection.side,
            protection.session_id,
        )
        payload = self._order_request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": self._normalize_symbol(protection.symbol),
                "side": "SELL" if protection.side == "buy" else "BUY",
                "type": "STOP_MARKET",
                "stopPrice": validation.normalized_stop_price,
                "quantity": validation.normalized_quantity,
                "reduceOnly": "true",
                "newClientOrderId": client_order_id,
            },
        )
        ack = _ack_from_binance(protection.session_id, client_order_id, payload, self.target)
        if ack.status != "accepted":
            raise RuntimeError("protective replacement was not confirmed working")
        self._active_protection = replace(
            protection,
            stop_client_order_id=client_order_id,
            stop_price=validation.normalized_stop_price,
        )
        cancellation = self._order_request(
            "DELETE",
            "/fapi/v1/order",
            {
                "symbol": self._normalize_symbol(protection.symbol),
                "origClientOrderId": protection.stop_client_order_id,
            },
        )
        if str(cancellation.get("status", "")).upper() != "CANCELED":
            raise RuntimeError("old protection cancellation was not confirmed")
        self._forget_order(protection.stop_client_order_id)
        return replace(
            ack, metadata={**ack.metadata, "stop_price": validation.normalized_stop_price}
        )

    def poll_fills(self, session_id: str) -> list[BrokerFill]:
        fills = [fill for fill in self._queued_fills if fill.session_id == session_id]
        self._queued_fills = [fill for fill in self._queued_fills if fill.session_id != session_id]
        for client_order_id, tracked in sorted(self._tracked_orders.items()):
            if tracked.session_id != session_id:
                continue
            payload = self._order_request(
                "GET",
                "/fapi/v1/order",
                {
                    "symbol": tracked.symbol,
                    "origClientOrderId": client_order_id,
                },
            )
            cumulative = _decimal_or_zero(payload.get("executedQty"))
            previous = self._cumulative_fills.get(client_order_id, Decimal("0"))
            delta = max(Decimal("0"), cumulative - previous)
            self._cumulative_fills[client_order_id] = cumulative
            fill = _fill_from_binance(session_id, payload, tracked, delta)
            if fill is not None:
                fill = self._annotate_venue_fees(fill)
                self._apply_fill(fill)
                fills.append(fill)
            if str(payload.get("status", "")).upper() in {
                "FILLED",
                "CANCELED",
                "EXPIRED",
                "REJECTED",
                "EXPIRED_IN_MATCH",
            }:
                self._forget_order(client_order_id)
        return fills

    def modify_protection(
        self,
        *,
        stop_price: float | None = None,
        target_price: float | None = None,
    ) -> BrokerOrderAck:
        protection = self._active_protection
        if protection is None:
            raise RuntimeError("no active protection to modify")
        replacement = replace(
            protection,
            stop_price=protection.stop_price if stop_price is None else str(stop_price),
            target_price=protection.target_price if target_price is None else str(target_price),
        )
        validate_protection_update(
            side=protection.side,
            current_stop=protection.stop_price,
            stop_price=replacement.stop_price,
            target_price=replacement.target_price,
        )
        replacement = _normalize_protective_order(
            replacement, self.fetch_metadata(protection.symbol)
        )
        validate_protection_update(
            side=protection.side,
            current_stop=protection.stop_price,
            stop_price=replacement.stop_price,
            target_price=replacement.target_price,
        )
        self._stop_revision += 1
        replacement = replace(
            replacement,
            stop_client_order_id=make_client_order_id(
                protection.session_id, f"protect-stop-{self._stop_revision}", self.target, "stop"
            ),
            target_client_order_id=make_client_order_id(
                protection.session_id,
                f"protect-target-{self._stop_revision}",
                self.target,
                "target",
            ),
        )
        # Keep both old reduce-only legs until both replacements are accepted.
        # Any transport uncertainty leaves every submitted ID tracked so the
        # engine's containment path can cancel and reconcile all of them.
        acks = self.place_protection(replacement)
        if len(acks) != 2 or any(ack.status != "accepted" for ack in acks):
            raise RuntimeError("protective replacement was not confirmed working")
        for identifier in (protection.stop_client_order_id, protection.target_client_order_id):
            payload = self._order_request(
                "DELETE",
                "/fapi/v1/order",
                {
                    "symbol": self._normalize_symbol(protection.symbol),
                    "origClientOrderId": identifier,
                },
            )
            if str(payload.get("status", "")).upper() != "CANCELED":
                raise RuntimeError("old protection cancellation was not confirmed")
            self._forget_order(identifier)
        return BrokerOrderAck(
            protection.session_id,
            replacement.stop_client_order_id,
            None,
            "accepted",
            self.target,
            metadata={
                "stop_price": replacement.stop_price,
                "target_price": replacement.target_price,
            },
        )

    def _annotate_venue_fees(self, fill: BrokerFill) -> BrokerFill:
        """Attach the venue commission and realized PnL of a completed fill.

        Fees paid in an asset other than the quote asset are recorded but never
        converted: the run is flagged approximated instead of guessing a rate.
        """
        if fill.status != "filled" or fill.exchange_order_id is None:
            return fill
        symbol = self._normalize_symbol(fill.symbol)
        payload = self._signed_request(
            "GET",
            "/fapi/v1/userTrades",
            {"symbol": symbol, "orderId": fill.exchange_order_id},
        )
        cached = self._metadata_by_symbol.get(symbol)
        quote = cached.quote_asset if cached and cached.quote_asset else _quote_asset(symbol)
        commission_quote = Decimal("0")
        realized = Decimal("0")
        unconverted = False
        for trade in _as_list(payload.get("data", payload)):
            amount = _decimal_or_zero(trade.get("commission"))
            if str(trade.get("commissionAsset", "")).upper() == quote:
                commission_quote += amount
            elif amount > 0:
                unconverted = True
            realized += _decimal_or_zero(trade.get("realizedPnl"))
        metadata = {
            **fill.metadata,
            "commission": str(commission_quote),
            "commission_asset": quote,
            "commission_unconverted": unconverted,
            "venue_realized_pnl": str(realized),
        }
        realized_pnl = fill.realized_pnl
        if fill.role in {"stop", "target", "flatten"}:
            realized_pnl = str(realized - (Decimal("0") if unconverted else commission_quote))
        return replace(fill, metadata=metadata, realized_pnl=realized_pnl)

    def _track_order(
        self,
        client_order_id: str,
        symbol: str,
        role: OrderRole,
        position_side: OrderSide,
        session_id: str,
    ) -> None:
        self._tracked_orders[client_order_id] = _TrackedOrder(
            symbol=self._normalize_symbol(symbol),
            role=role,
            position_side=position_side,
            session_id=session_id,
        )

    def _forget_order(self, client_order_id: str) -> None:
        self._tracked_orders.pop(client_order_id, None)
        self._cumulative_fills.pop(client_order_id, None)
        self._algo_router.forget(client_order_id)

    def _apply_fill(self, fill: BrokerFill) -> None:
        quantity = _decimal_or_zero(fill.quantity)
        if quantity <= 0:
            return
        if fill.role == "entry":
            current = self._position
            price = _decimal_or_zero(fill.price)
            if current is None:
                self._position = BrokerPositionSnapshot(
                    symbol=fill.symbol,
                    side=fill.side,
                    quantity=str(quantity),
                    entry_price=str(price),
                )
                return
            current_quantity = _decimal_or_zero(current.quantity)
            total = current_quantity + quantity
            current_price = _decimal_or_zero(current.entry_price)
            average = (
                (current_price * current_quantity + price * quantity) / total
                if total > 0
                else Decimal("0")
            )
            self._position = BrokerPositionSnapshot(
                symbol=current.symbol,
                side=current.side,
                quantity=str(total),
                entry_price=str(average),
                position_id=current.position_id,
                metadata=current.metadata,
            )
            return
        current = self._position
        if current is None:
            return
        remaining = _decimal_or_zero(current.quantity) - quantity
        if remaining <= 0:
            self._position = None
            return
        self._position = BrokerPositionSnapshot(
            symbol=current.symbol,
            side=current.side,
            quantity=str(remaining),
            entry_price=current.entry_price,
            position_id=current.position_id,
            metadata=current.metadata,
        )

    def reconcile(
        self, session_id: str, intents: list[BrokerOrderIntent]
    ) -> BrokerReconciliationReport:
        self._ensure_one_way_mode()
        for intent in intents:
            if self._conditional_order_api == "algo" and (
                intent.role in {"stop", "target"}
                or intent.order_type.lower() in {"stop", "stop_market"}
            ):
                self._algo_router.track(intent.client_order_id)
            self._track_order(
                intent.client_order_id,
                intent.symbol,
                intent.role,
                intent.side,
                session_id,
            )
        fills = self.poll_fills(session_id) if self._tracked_orders else []
        positions = self._fetch_positions()
        self._position = positions[0] if len(positions) == 1 else None
        open_order_payloads = _as_list(
            self._signed_request("GET", "/fapi/v1/openOrders", {}).get("data", [])
        )
        if self._conditional_order_api == "algo":
            open_order_payloads.extend(self._algo_router.open_orders())
        open_orders: list[BrokerOrderAck] = []
        incidents: list[str] = []
        position_symbols = {position.symbol for position in positions}
        protective_types_by_symbol: dict[str, set[str]] = {}
        for item in open_order_payloads:
            client_order_id = str(item.get("clientOrderId", ""))
            symbol = self._normalize_symbol(str(item.get("symbol", "")))
            reduce_only = str(item.get("reduceOnly", "")).lower() == "true"
            order_type = str(item.get("type", "")).upper()
            tracked = self._tracked_orders.get(client_order_id)
            if tracked is not None:
                role = tracked.role
                position_side = tracked.position_side
            elif reduce_only:
                role = "stop" if order_type.startswith("STOP") else "target"
                position_side = "buy" if str(item.get("side", "")).upper() == "SELL" else "sell"
            else:
                role = "entry"
                position_side = "buy" if str(item.get("side", "")).upper() == "BUY" else "sell"
            if client_order_id:
                self._track_order(
                    client_order_id,
                    symbol,
                    role,
                    position_side,
                    session_id,
                )
            open_orders.append(_ack_from_binance(session_id, client_order_id, item, self.target))
            if reduce_only:
                protective_types_by_symbol.setdefault(symbol, set()).add(order_type)
                if symbol not in position_symbols:
                    incidents.append(f"orphan_protective_order:{symbol}:{client_order_id}")
            else:
                incidents.append(f"working_entry_order:{symbol}:{client_order_id}")
        for position in positions:
            incidents.append(f"existing_position:{position.symbol}")
            order_types = protective_types_by_symbol.get(position.symbol, set())
            if not {"STOP_MARKET", "TAKE_PROFIT_MARKET"} <= order_types:
                incidents.append(f"unprotected_position:{position.symbol}")
        return BrokerReconciliationReport(
            session_id=session_id,
            target=self.target,
            open_orders=open_orders,
            fills=fills,
            positions=positions,
            incidents=incidents,
        )

    def cancel_all(self, symbol: str, session_id: str) -> list[BrokerOrderAck]:
        normalized = self._normalize_symbol(symbol)
        matching = [
            (client_order_id, tracked)
            for client_order_id, tracked in self._tracked_orders.items()
            if tracked.symbol == normalized and tracked.session_id == session_id
        ]
        acks: list[BrokerOrderAck] = []
        first_error: Exception | None = None
        for client_order_id, tracked in matching:
            try:
                payload = self._order_request(
                    "DELETE",
                    "/fapi/v1/order",
                    {
                        "symbol": normalized,
                        "origClientOrderId": client_order_id,
                    },
                )
            except Exception as exc:  # every known session order still gets an attempt
                if first_error is None:
                    first_error = exc
                continue
            ack = _ack_from_binance(session_id, client_order_id, payload, self.target)
            acks.append(ack)
            cumulative = _decimal_or_zero(payload.get("executedQty"))
            previous = self._cumulative_fills.get(client_order_id, Decimal("0"))
            delta = max(Decimal("0"), cumulative - previous)
            self._cumulative_fills[client_order_id] = cumulative
            fill = _fill_from_binance(session_id, payload, tracked, delta)
            if fill is not None and delta > 0:
                self._apply_fill(fill)
                self._queued_fills.append(fill)
            if ack.status in {"canceled", "filled"}:
                self._forget_order(client_order_id)
        if not any(order.role in {"stop", "target"} for order in self._tracked_orders.values()):
            self._active_protection = None
        if first_error is not None:
            raise first_error
        return acks

    def flatten(self, symbol: str, session_id: str) -> BrokerOrderAck | None:
        self._ensure_one_way_mode()
        position = next(iter(self._fetch_positions(symbol)), None)
        self._position = position
        if position is None:
            return None
        quantity = _abs_quantity(position.quantity)
        if quantity is None:
            return None
        side = "SELL" if position.side == "buy" else "BUY"
        client_order_id = make_client_order_id(
            session_id, f"flatten-{position.symbol}", self.target, "flatten"
        )
        self._track_order(
            client_order_id,
            position.symbol,
            "flatten",
            position.side,
            session_id,
        )
        payload = self._order_request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": position.symbol,
                "side": side,
                "type": "MARKET",
                "quantity": quantity,
                "reduceOnly": "true",
                "newClientOrderId": client_order_id,
                "newOrderRespType": "RESULT",
            },
        )
        ack = _ack_from_binance(session_id, client_order_id, payload, self.target)
        if ack.status == "filled":
            tracked = self._tracked_orders[client_order_id]
            quantity = _decimal_or_zero(payload.get("executedQty"))
            fill = _fill_from_binance(session_id, payload, tracked, quantity)
            if (
                fill is None
                or _decimal_or_zero(fill.quantity) <= 0
                or _decimal_or_zero(fill.price) <= 0
            ):
                raise RuntimeError(
                    "filled flatten response omitted a positive quantity or average price"
                )
            self._apply_fill(fill)
            self._queued_fills.append(fill)
            self._forget_order(client_order_id)
        elif ack.status != "accepted":
            self._forget_order(client_order_id)
        return ack

    def _fetch_positions(self, symbol: str | None = None) -> list[BrokerPositionSnapshot]:
        params = {}
        if symbol is not None:
            params["symbol"] = self._normalize_symbol(symbol)
        payload = self._signed_request("GET", "/fapi/v2/positionRisk", params)
        return [
            position
            for item in _as_list(payload.get("data", payload))
            if (position := _position_from_binance(item)) is not None
        ]

    def _signed_request(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {
            **params,
            "timestamp": int(self._clock_ms()) + self._clock_skew_ms,
            "recvWindow": _SIGNED_RECV_WINDOW_MS,
        }
        encoded = urlencode(query)
        signature = hmac_sha256(self._api_secret, encoded)
        signed_query = f"{encoded}&signature={signature}"
        headers = {"X-MBX-APIKEY": self._api_key}
        try:
            response = self._request(
                method,
                f"{self.base_url}{path}",
                params=signed_query,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            exc.args = (safe_exception_message(exc),)
            exc.request = None
            exc.response = None
            raise
        metadata = redact_mapping(
            {
                "request": {
                    "method": method,
                    "path": path,
                    "params": query,
                    "headers": headers,
                },
                "response": data,
            }
        )
        if isinstance(data, list):
            return {"data": data, "metadata": metadata}
        return {**data, "metadata": metadata}

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        response = self._session.request(
            method,
            url,
            timeout=self._timeout,
            allow_redirects=False,
            **kwargs,
        )
        if response.is_redirect or response.is_permanent_redirect or response.history:
            raise RuntimeError("sandbox request redirect refused")
        if not response.url.startswith(f"{self.base_url}/"):
            raise RuntimeError("sandbox response escaped the allowlisted origin")
        return response


def _positive_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _quantize_to_step(value: Decimal, step: Decimal, *, rounding: str) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


def _normalize_entry_intent(
    intent: BrokerOrderIntent,
    metadata: VenueSymbolMetadata,
) -> BrokerOrderIntent:
    quantity = _positive_decimal(intent.quantity)
    price = _positive_decimal(intent.price)
    stop_price = _positive_decimal(intent.stop_price)
    target_price = _positive_decimal(intent.target_price)
    quantity_step = _positive_decimal(metadata.market_quantity_step or metadata.quantity_step)
    price_tick = _positive_decimal(metadata.price_tick)
    if quantity is None or price is None or quantity_step is None or price_tick is None:
        return intent

    normalized_quantity = _quantize_to_step(
        quantity,
        quantity_step,
        rounding=ROUND_DOWN,
    )
    # A working entry never crosses further than the strategy asked for: a buy
    # rounds down to the tick, a sell rounds up.
    normalized_price = _quantize_to_step(
        price, price_tick, rounding=ROUND_DOWN if intent.side == "buy" else ROUND_UP
    )
    normalized_stop = stop_price
    normalized_target = target_price
    if stop_price is not None:
        normalized_stop = _quantize_to_step(
            stop_price,
            price_tick,
            rounding=ROUND_UP if intent.side == "buy" else ROUND_DOWN,
        )
    if target_price is not None:
        normalized_target = _quantize_to_step(
            target_price,
            price_tick,
            rounding=ROUND_DOWN if intent.side == "buy" else ROUND_UP,
        )
    return replace(
        intent,
        quantity=str(normalized_quantity),
        price=str(normalized_price),
        stop_price=None if normalized_stop is None else str(normalized_stop),
        target_price=None if normalized_target is None else str(normalized_target),
    )


def _normalize_protective_order(
    intent: ProtectiveOrderIntent,
    metadata: VenueSymbolMetadata,
) -> ProtectiveOrderIntent:
    quantity = _positive_decimal(intent.quantity)
    stop_price = _positive_decimal(intent.stop_price)
    target_price = _positive_decimal(intent.target_price)
    quantity_step = _positive_decimal(metadata.market_quantity_step or metadata.quantity_step)
    price_tick = _positive_decimal(metadata.price_tick)
    if (
        quantity is None
        or stop_price is None
        or target_price is None
        or quantity_step is None
        or price_tick is None
    ):
        raise ValueError("invalid protective order: venue metadata is unusable")
    normalized_quantity = _quantize_to_step(
        quantity,
        quantity_step,
        rounding=ROUND_DOWN,
    )
    normalized_stop = _quantize_to_step(
        stop_price,
        price_tick,
        rounding=ROUND_UP if intent.side == "buy" else ROUND_DOWN,
    )
    normalized_target = _quantize_to_step(
        target_price,
        price_tick,
        rounding=ROUND_DOWN if intent.side == "buy" else ROUND_UP,
    )
    return replace(
        intent,
        quantity=str(normalized_quantity),
        stop_price=str(normalized_stop),
        target_price=str(normalized_target),
    )


def _order_type(order_type: str) -> str:
    if order_type == "market":
        return "MARKET"
    if order_type == "stop":
        return "STOP_MARKET"
    return order_type.upper()


def _quote_asset(symbol: str) -> str:
    for quote in ("USDT", "USDC", "BUSD"):
        if symbol.upper().endswith(quote):
            return quote
    return "USDT"


def _validate_protective_order(
    intent: ProtectiveOrderIntent,
    *,
    target: str,
) -> None:
    if intent.target != target:
        raise ValueError("invalid protective order: target does not match the broker")
    if intent.side not in {"buy", "sell"}:
        raise ValueError("invalid protective order: side must be 'buy' or 'sell'")
    if not intent.reduce_only:
        raise ValueError("invalid protective order: reduce_only must be true")
    if (
        not intent.session_id
        or not intent.symbol
        or not intent.stop_client_order_id
        or not intent.target_client_order_id
        or intent.stop_client_order_id == intent.target_client_order_id
    ):
        raise ValueError("invalid protective order: identifiers and symbol are required")
    try:
        quantity = Decimal(intent.quantity)
        stop_price = Decimal(intent.stop_price)
        target_price = Decimal(intent.target_price)
    except InvalidOperation as exc:
        raise ValueError("invalid protective order: prices and quantity must be numeric") from exc
    if any(not value.is_finite() or value <= 0 for value in (quantity, stop_price, target_price)):
        raise ValueError(
            "invalid protective order: prices and quantity must be positive and finite"
        )
    if intent.side == "buy" and stop_price >= target_price:
        raise ValueError("invalid protective order: long stop must be below target")
    if intent.side == "sell" and target_price >= stop_price:
        raise ValueError("invalid protective order: short target must be below stop")


def _ack_from_binance(
    session_id: str, client_order_id: str, payload: dict[str, Any], target: str
) -> BrokerOrderAck:
    exchange_status = str(payload.get("status", "")).upper()
    if exchange_status in {"NEW", "PARTIALLY_FILLED"}:
        status = "accepted"
    elif exchange_status == "FILLED":
        status = "filled"
    elif exchange_status == "CANCELED":
        status = "canceled"
    else:
        status = "rejected"
    return BrokerOrderAck(
        session_id=session_id,
        client_order_id=str(payload.get("clientOrderId") or client_order_id),
        exchange_order_id=None if payload.get("orderId") is None else str(payload["orderId"]),
        status=status,  # type: ignore[arg-type]
        target=target,
        metadata=payload.get("metadata", {}),
    )


def _fill_from_binance(
    session_id: str,
    payload: dict[str, Any],
    tracked: _TrackedOrder,
    delta: Decimal,
) -> BrokerFill | None:
    status = str(payload.get("status", "")).upper()
    if status not in {
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "REJECTED",
        "EXPIRED_IN_MATCH",
    }:
        return None
    mapped = {
        "PARTIALLY_FILLED": "partial",
        "FILLED": "filled",
        "CANCELED": "canceled",
        "EXPIRED": "expired",
        "REJECTED": "expired",
        "EXPIRED_IN_MATCH": "expired",
    }[status]
    if status in {"PARTIALLY_FILLED", "FILLED"} and delta <= 0:
        return None
    return BrokerFill(
        session_id=session_id,
        client_order_id=str(payload.get("clientOrderId", "")),
        exchange_order_id=None if payload.get("orderId") is None else str(payload["orderId"]),
        symbol=str(payload.get("symbol", "")),
        side=tracked.position_side,
        status=mapped,  # type: ignore[arg-type]
        role=tracked.role,
        quantity=str(delta) if delta > 0 else None,
        price=str(payload.get("avgPrice") or payload.get("price") or "0"),
        timestamp_ms=int(payload.get("updateTime") or int(time.time() * 1000)),
        metadata=payload.get("metadata", {}),
    )


def _position_from_binance(item: dict[str, Any]) -> BrokerPositionSnapshot | None:
    try:
        amount = Decimal(str(item.get("positionAmt") or "0"))
    except InvalidOperation:
        return None
    if amount == 0:
        return None
    return BrokerPositionSnapshot(
        symbol=str(item.get("symbol", "")),
        side="buy" if amount > 0 else "sell",
        quantity=str(abs(amount)),
        entry_price=None if item.get("entryPrice") is None else str(item["entryPrice"]),
        metadata=redact_mapping(item),  # type: ignore[arg-type]
    )


def _abs_quantity(quantity: str) -> str | None:
    try:
        return str(abs(Decimal(quantity)))
    except InvalidOperation:
        return None


def _decimal_or_zero(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        return value["data"]
    if isinstance(value, list):
        return value
    return []
