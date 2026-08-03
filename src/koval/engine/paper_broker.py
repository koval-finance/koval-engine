"""Simulated paper broker (MIT) - bracket / OCO fills against bar OHLC.

FILL RULESET (single source of truth for paper fills):
- Market entry fills at the close of the signal bar (``fill_market_if_pending``).
- Limit entry (``limit`` or ``limit_at_zone``) fills on a later bar when
  ``low <= limit <= high``, at the limit price.
- Stop entry (``stop`` or ``stop_market``) fills when price trades through the
  trigger. A gap through the trigger fills at the bar open.
- Stop-loss fills when ``low <= SL`` (long) / ``high >= SL`` (short), at SL.
- A position carried into a bar that gaps through its stop fills at the bar
  open, modelling adverse stop slippage.
- Take-profit fills when ``high >= TP`` (long) / ``low <= TP`` (short), at TP.
- Protection is active on a limit/stop entry's fill bar. Because OHLC does not
  reveal intrabar ordering, a bar crossing both SL and TP is resolved SL-first.
- No order-book slippage model is applied beyond gap-through stops. Results are
  an idealized deterministic simulation, not a guarantee or performance bound.
Equity = realized balance + unrealized PnL of the open position marked to the
last seen close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from koval.engine.broker import (
    BrokerFill,
    BrokerOrderAck,
    BrokerOrderIntent,
    BrokerReconciliationReport,
    ProtectiveOrderIntent,
)

_MARKET_ORDER_TYPES = frozenset({"market"})
_LIMIT_ORDER_TYPES = frozenset({"limit", "limit_at_zone"})
_STOP_ORDER_TYPES = frozenset({"stop", "stop_market"})


@dataclass(frozen=True)
class Fill:
    kind: str  # "entry" | "stop_loss" | "take_profit" | "manual_close"
    side: str  # position side: "buy" | "sell"
    price: float
    quantity: float
    timestamp_ms: int
    realized_pnl: float


@dataclass(frozen=True)
class BrokerPosition:
    side: str
    entry_price: float
    quantity: float
    stop_price: float
    target_price: float
    entry_timestamp_ms: int
    session_id: str = ""
    entry_client_order_id: str = ""
    symbol: str = ""
    stop_client_order_id: str = ""
    target_client_order_id: str = ""


@dataclass
class _PendingOrder:
    side: str
    entry_price: float
    stop_price: float
    target_price: float
    quantity: float
    order_type: str
    session_id: str = ""
    client_order_id: str = ""
    symbol: str = ""


class PaperBroker:
    target = "paper"

    def __init__(self, starting_balance: float) -> None:
        try:
            balance = float(starting_balance)
        except (TypeError, ValueError) as exc:
            raise ValueError("paper starting balance must be positive and finite") from exc
        if not math.isfinite(balance) or balance <= 0:
            raise ValueError("paper starting balance must be positive and finite")
        self._balance = balance
        self._position: BrokerPosition | None = None
        self._pending: _PendingOrder | None = None
        self._last_close = 0.0
        self._last_timestamp_ms = 0
        self._fill_events: list[BrokerFill] = []
        self._deferred_exit: tuple[Fill, BrokerPosition, str] | None = None

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def position(self) -> BrokerPosition | None:
        return self._position

    @property
    def pending(self) -> bool:
        return self._pending is not None

    @property
    def equity(self) -> float:
        return self._balance + self._unrealized(self._last_close)

    def _unrealized(self, price: float) -> float:
        p = self._position
        if p is None:
            return 0.0
        sign = 1.0 if p.side == "buy" else -1.0
        return (price - p.entry_price) * p.quantity * sign

    def submit_bracket(
        self,
        *,
        side: str,
        entry_price: float,
        stop_price: float,
        target_price: float,
        quantity: float,
        order_type: str,
        session_id: str = "",
        client_order_id: str = "",
        symbol: str = "",
    ) -> None:
        entry_price = float(entry_price)
        stop_price = float(stop_price)
        target_price = float(target_price)
        quantity = float(quantity)
        if side not in {"buy", "sell"}:
            raise ValueError("invalid paper order: side must be 'buy' or 'sell'")
        if order_type not in _MARKET_ORDER_TYPES | _LIMIT_ORDER_TYPES | _STOP_ORDER_TYPES:
            raise ValueError("invalid paper order: unsupported order type")
        values = (entry_price, stop_price, target_price, quantity)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("invalid paper order: prices and quantity must be positive and finite")
        if side == "buy" and not stop_price < entry_price < target_price:
            raise ValueError(
                "invalid paper order: long protection must satisfy stop < entry < target"
            )
        if side == "sell" and not target_price < entry_price < stop_price:
            raise ValueError(
                "invalid paper order: short protection must satisfy target < entry < stop"
            )
        self._pending = _PendingOrder(
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            quantity=quantity,
            order_type=order_type,
            session_id=session_id,
            client_order_id=client_order_id,
            symbol=symbol,
        )

    def submit_entry(self, intent: BrokerOrderIntent) -> BrokerOrderAck:
        self.submit_bracket(
            side=intent.side,
            entry_price=float(intent.price or 0.0),
            stop_price=float(intent.stop_price or 0.0),
            target_price=float(intent.target_price or 0.0),
            quantity=float(intent.quantity),
            order_type=intent.order_type,
            session_id=intent.session_id,
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
        )
        return BrokerOrderAck(
            session_id=intent.session_id,
            client_order_id=intent.client_order_id,
            exchange_order_id=None,
            status="accepted",
            target=self.target,
            metadata={"paper": True},
        )

    def fill_market_if_pending(self, *, ts_ms: int, price: float) -> Fill | None:
        o = self._pending
        if o is None or o.order_type not in _MARKET_ORDER_TYPES:
            return None
        return self._open(o, fill_price=float(price), ts_ms=ts_ms)

    def _open(self, o: _PendingOrder, *, fill_price: float, ts_ms: int) -> Fill:
        self._position = BrokerPosition(
            side=o.side,
            entry_price=fill_price,
            quantity=o.quantity,
            stop_price=o.stop_price,
            target_price=o.target_price,
            entry_timestamp_ms=int(ts_ms),
            session_id=o.session_id,
            entry_client_order_id=o.client_order_id,
            symbol=o.symbol,
        )
        self._pending = None
        self._last_close = fill_price
        self._last_timestamp_ms = int(ts_ms)
        fill = Fill("entry", o.side, fill_price, o.quantity, int(ts_ms), 0.0)
        self._record_broker_fill(fill, o.session_id, o.client_order_id, o.symbol, role="entry")
        return fill

    def modify_stop(self, new_stop: float) -> None:
        if self._position is not None:
            p = self._position
            new_stop = float(new_stop)
            if not math.isfinite(new_stop) or new_stop <= 0:
                raise ValueError("stop update must be positive and finite")
            if (p.side == "buy" and new_stop < p.stop_price) or (
                p.side == "sell" and new_stop > p.stop_price
            ):
                raise ValueError("stop update must tighten protection, not increase risk")
            if (p.side == "buy" and new_stop >= p.target_price) or (
                p.side == "sell" and new_stop <= p.target_price
            ):
                raise ValueError("stop update must remain inside the protective target")
            self._position = replace(p, stop_price=new_stop)

    def place_protection(self, intent: ProtectiveOrderIntent) -> list[BrokerOrderAck]:
        reference = self._validate_protection_intent(intent)
        if self._position is not None:
            self._position = replace(
                reference,
                stop_price=float(intent.stop_price),
                target_price=float(intent.target_price),
                session_id=intent.session_id,
                entry_client_order_id=intent.entry_client_order_id,
                symbol=intent.symbol,
                stop_client_order_id=intent.stop_client_order_id,
                target_client_order_id=intent.target_client_order_id,
            )
        self._record_deferred_exit(intent)
        return [
            BrokerOrderAck(
                session_id=intent.session_id,
                client_order_id=intent.stop_client_order_id,
                exchange_order_id=None,
                status="accepted",
                target=self.target,
                metadata={"paper": True, "reduce_only": intent.reduce_only},
            ),
            BrokerOrderAck(
                session_id=intent.session_id,
                client_order_id=intent.target_client_order_id,
                exchange_order_id=None,
                status="accepted",
                target=self.target,
                metadata={"paper": True, "reduce_only": intent.reduce_only},
            ),
        ]

    def _validate_protection_intent(self, intent: ProtectiveOrderIntent) -> BrokerPosition:
        reference = (
            self._position
            if self._position is not None
            else self._deferred_exit[1]
            if self._deferred_exit is not None
            else None
        )
        if reference is None:
            raise ValueError("invalid protective order: no position to protect")
        if intent.target != self.target:
            raise ValueError("invalid protective order: target does not match the broker")
        if intent.side not in {"buy", "sell"} or intent.side != reference.side:
            raise ValueError("invalid protective order: side does not match the position")
        if not intent.reduce_only:
            raise ValueError("invalid protective order: reduce_only must be true")
        identifiers = (
            intent.session_id,
            intent.entry_client_order_id,
            intent.stop_client_order_id,
            intent.target_client_order_id,
            intent.symbol,
        )
        if any(not value for value in identifiers) or len(set(identifiers[1:4])) != 3:
            raise ValueError(
                "invalid protective order: identifiers and symbol are required and unique"
            )
        if reference.session_id and intent.session_id != reference.session_id:
            raise ValueError("invalid protective order: session does not match the position")
        if (
            reference.entry_client_order_id
            and intent.entry_client_order_id != reference.entry_client_order_id
        ):
            raise ValueError("invalid protective order: entry does not match the position")
        if reference.symbol and intent.symbol != reference.symbol:
            raise ValueError("invalid protective order: symbol does not match the position")
        try:
            quantity = float(intent.quantity)
            stop_price = float(intent.stop_price)
            target_price = float(intent.target_price)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "invalid protective order: prices and quantity must be numeric"
            ) from exc
        if any(
            not math.isfinite(value) or value <= 0 for value in (quantity, stop_price, target_price)
        ):
            raise ValueError(
                "invalid protective order: prices and quantity must be positive and finite"
            )
        if quantity != reference.quantity:
            raise ValueError("invalid protective order: quantity does not match the position")
        if intent.side == "buy":
            if stop_price < reference.stop_price:
                raise ValueError("invalid protective order: stop must not increase position risk")
            if not stop_price < reference.entry_price < target_price:
                raise ValueError(
                    "invalid protective order: long protection must satisfy stop < entry < target"
                )
        else:
            if stop_price > reference.stop_price:
                raise ValueError("invalid protective order: stop must not increase position risk")
            if not target_price < reference.entry_price < stop_price:
                raise ValueError(
                    "invalid protective order: short protection must satisfy target < entry < stop"
                )
        return reference

    def process_bar(  # noqa: A002
        self,
        *,
        ts_ms: int,
        open: float,  # noqa: A002
        high: float,
        low: float,
        close: float,
    ) -> list[Fill]:
        self._last_close = float(close)
        self._last_timestamp_ms = int(ts_ms)
        # 1. pending limit/stop entry fills against this bar
        if self._pending is not None and self._position is None:
            o = self._pending
            fill_price = self._entry_cross(o, open, high, low)
            if fill_price is not None:
                entry_fill = self._open(o, fill_price=fill_price, ts_ms=ts_ms)
                self._last_close = float(close)
                self._last_timestamp_ms = int(ts_ms)
                exit_fill = self._exit_cross(
                    open=open,
                    high=high,
                    low=low,
                    ts_ms=ts_ms,
                    allow_open_gap=False,
                )
                return [entry_fill, exit_fill] if exit_fill is not None else [entry_fill]
            return []
        # 2. open position: SL / TP (SL first on ambiguity)
        exit_fill = self._exit_cross(
            open=open,
            high=high,
            low=low,
            ts_ms=ts_ms,
            allow_open_gap=True,
        )
        return [exit_fill] if exit_fill is not None else []

    def _exit_cross(
        self,
        *,
        open: float,
        high: float,
        low: float,
        ts_ms: int,
        allow_open_gap: bool,
    ) -> Fill | None:
        p = self._position
        if p is None:
            return None
        is_long = p.side == "buy"
        if allow_open_gap and (
            (is_long and open <= p.stop_price) or (not is_long and open >= p.stop_price)
        ):
            return self._close(p, exit_price=open, kind="stop_loss", ts_ms=ts_ms)
        hit_sl = low <= p.stop_price if is_long else high >= p.stop_price
        hit_tp = high >= p.target_price if is_long else low <= p.target_price
        if hit_sl:
            return self._close(p, exit_price=p.stop_price, kind="stop_loss", ts_ms=ts_ms)
        if hit_tp:
            return self._close(p, exit_price=p.target_price, kind="take_profit", ts_ms=ts_ms)
        return None

    def _entry_cross(
        self,
        o: _PendingOrder,
        open: float,
        high: float,
        low: float,
    ) -> float | None:
        if o.order_type in _LIMIT_ORDER_TYPES:
            return o.entry_price if low <= o.entry_price <= high else None
        if o.order_type in _STOP_ORDER_TYPES:
            if (o.side == "buy" and open >= o.entry_price) or (
                o.side == "sell" and open <= o.entry_price
            ):
                return float(open)
            crossed = high >= o.entry_price if o.side == "buy" else low <= o.entry_price
            return o.entry_price if crossed else None
        return None

    def _close(self, p: BrokerPosition, *, exit_price: float, kind: str, ts_ms: int) -> Fill:
        sign = 1.0 if p.side == "buy" else -1.0
        pnl = (exit_price - p.entry_price) * p.quantity * sign
        self._balance += pnl
        self._position = None
        self._last_close = float(exit_price)
        self._last_timestamp_ms = int(ts_ms)
        fill = Fill(kind, p.side, float(exit_price), p.quantity, int(ts_ms), pnl)
        self._record_close_broker_fill(fill, p)
        return fill

    def flatten(
        self, *args, ts_ms: int | None = None, price: float | None = None
    ) -> Fill | BrokerOrderAck | None:
        if len(args) == 2 and ts_ms is None and price is None:
            session_id = str(args[1])
            self._pending = None
            if self._position is None:
                return None
            client_order_id = f"paper-flatten-{session_id}"
            fill = self._close(
                self._position,
                exit_price=self._last_close,
                kind="manual_close",
                ts_ms=self._last_timestamp_ms,
            )
            return BrokerOrderAck(
                session_id=session_id,
                client_order_id=client_order_id,
                exchange_order_id=None,
                status="filled",
                target=self.target,
                metadata={"paper": True, "fill_price": fill.price},
            )
        if ts_ms is None or price is None:
            raise TypeError("paper flatten requires either (symbol, session_id) or ts_ms/price")
        self._pending = None
        if self._position is None:
            return None
        return self._close(
            self._position, exit_price=float(price), kind="manual_close", ts_ms=ts_ms
        )

    def poll_fills(self, session_id: str) -> list[BrokerFill]:
        fills = [fill for fill in self._fill_events if fill.session_id == session_id]
        self._fill_events = [fill for fill in self._fill_events if fill.session_id != session_id]
        return fills

    def reconcile(
        self, session_id: str, intents: list[BrokerOrderIntent]
    ) -> BrokerReconciliationReport:
        return BrokerReconciliationReport(session_id=session_id, target=self.target)

    def cancel_all(self, symbol: str, session_id: str) -> list[BrokerOrderAck]:
        had_pending = self._pending is not None
        self._pending = None
        return [
            BrokerOrderAck(
                session_id=session_id,
                client_order_id=f"paper-cancel-{session_id}",
                exchange_order_id=None,
                status="canceled" if had_pending else "accepted",
                target=self.target,
                metadata={"paper": True, "symbol": symbol},
            )
        ]

    def _record_close_broker_fill(self, fill: Fill, position: BrokerPosition) -> None:
        role = {
            "stop_loss": "stop",
            "take_profit": "target",
            "manual_close": "flatten",
        }[fill.kind]
        client_order_id = {
            "stop": position.stop_client_order_id,
            "target": position.target_client_order_id,
            "flatten": f"paper-flatten-{position.session_id}",
        }[role]
        if role in {"stop", "target"} and not client_order_id:
            if position.session_id and position.entry_client_order_id:
                self._deferred_exit = (fill, position, role)
            return
        self._record_broker_fill(
            fill,
            position.session_id,
            client_order_id,
            position.symbol,
            role=role,
        )

    def _record_deferred_exit(self, intent: ProtectiveOrderIntent) -> None:
        deferred = self._deferred_exit
        if deferred is None:
            return
        fill, position, role = deferred
        if (
            position.session_id != intent.session_id
            or position.entry_client_order_id != intent.entry_client_order_id
        ):
            return
        client_order_id = (
            intent.stop_client_order_id if role == "stop" else intent.target_client_order_id
        )
        self._record_broker_fill(
            fill,
            intent.session_id,
            client_order_id,
            intent.symbol,
            role=role,
        )
        self._deferred_exit = None

    def _record_broker_fill(
        self,
        fill: Fill,
        session_id: str,
        client_order_id: str,
        symbol: str,
        *,
        role: str,
    ) -> None:
        if not session_id or not client_order_id:
            return
        self._fill_events.append(
            BrokerFill(
                session_id=session_id,
                client_order_id=client_order_id,
                exchange_order_id=None,
                symbol=symbol,
                side=fill.side,
                status="filled",
                role=role,  # type: ignore[arg-type]
                quantity=str(fill.quantity),
                price=str(fill.price),
                timestamp_ms=fill.timestamp_ms,
                realized_pnl=str(fill.realized_pnl),
                metadata={"paper": True, "kind": fill.kind},
            )
        )
