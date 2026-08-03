"""Live paper and exchange-sandbox strategy engine.

Runs an assembled strategy against a broker and a closed-bar feed while keeping
account state, order journals, fills, protection, and domain callbacks aligned.
The module is host-framework agnostic and has no real-money execution path.
"""

from __future__ import annotations

import datetime as _dt
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from koval.engine.account_state import PlatformAccountState
from koval.engine.broker import (
    Broker,
    BrokerFill,
    BrokerOrderAck,
    BrokerOrderIntent,
    ProtectiveOrderIntent,
)
from koval.engine.client_order_id import make_client_order_id
from koval.engine.engine_events import EngineEvent, EventType
from koval.engine.live_feed import LiveFeed, StopSignal
from koval.engine.paper_broker import Fill, PaperBroker
from koval.exchanges.auth import safe_exception_message
from koval.exchanges.base import timeframe_ms
from koval.strategy.base.trade_setup import TradeSetup
from koval.strategy.block_assembler import assemble_from_graph

_DEFAULT_MAX_WINDOW = 2000


@dataclass
class LiveEngineConfig:
    symbol: str
    timeframe: str
    initial_capital: float
    history: np.ndarray | None = None
    max_window: int = _DEFAULT_MAX_WINDOW


def _noop(_x: object) -> None:
    return None


def _iso(ts_ms: int | None) -> str:
    if not ts_ms:
        return ""
    return _dt.datetime.fromtimestamp(int(ts_ms) / 1000, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _intent_to_dict(intent: BrokerOrderIntent) -> dict:
    return {
        "session_id": intent.session_id,
        "intent_id": intent.intent_id,
        "client_order_id": intent.client_order_id,
        "symbol": intent.symbol,
        "side": intent.side,
        "order_type": intent.order_type,
        "quantity": intent.quantity,
        "price": intent.price,
        "stop_price": intent.stop_price,
        "target_price": intent.target_price,
        "target": intent.target,
        "role": intent.role,
        "metadata": dict(intent.metadata),
    }


def _ack_to_dict(ack: BrokerOrderAck) -> dict:
    return {
        "session_id": ack.session_id,
        "client_order_id": ack.client_order_id,
        "exchange_order_id": ack.exchange_order_id,
        "status": ack.status,
        "target": ack.target,
        "metadata": dict(ack.metadata),
    }


def _broker_fill_to_dict(fill: BrokerFill) -> dict:
    return {
        "session_id": fill.session_id,
        "client_order_id": fill.client_order_id,
        "exchange_order_id": fill.exchange_order_id,
        "symbol": fill.symbol,
        "side": fill.side,
        "status": fill.status,
        "role": fill.role,
        "quantity": fill.quantity,
        "price": fill.price,
        "timestamp_ms": fill.timestamp_ms,
        "realized_pnl": fill.realized_pnl,
        "metadata": dict(fill.metadata),
    }


def _broker_fill_to_paper_fill(fill: BrokerFill) -> Fill | None:
    if fill.quantity is None or fill.price is None:
        return None
    return Fill(
        kind="entry" if fill.role == "entry" else fill.role,
        side=fill.side,
        price=float(fill.price),
        quantity=float(fill.quantity),
        timestamp_ms=fill.timestamp_ms,
        realized_pnl=float(fill.realized_pnl or 0.0),
    )


def _validate_ohlcv_row(
    row: object,
    *,
    previous_timestamp_ms: int | None,
    timeframe: str,
) -> np.ndarray:
    try:
        values = np.asarray(row, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("OHLCV row must contain exactly six numeric values") from exc
    if values.shape != (6,):
        raise ValueError("OHLCV row must contain exactly six numeric values")
    if not np.isfinite(values).all():
        raise ValueError("OHLCV row values must be finite")

    timestamp = float(values[0])
    if timestamp < 0 or not timestamp.is_integer():
        raise ValueError("OHLCV timestamp must be a non-negative integer")
    open_price, high, low, close, volume = (float(value) for value in values[1:])
    if any(price <= 0 for price in (open_price, high, low, close)):
        raise ValueError("OHLCV prices must be positive")
    if volume < 0:
        raise ValueError("OHLCV volume must be non-negative")
    if high < max(open_price, low, close) or low > min(open_price, high, close):
        raise ValueError("OHLCV high/low bounds are invalid")

    timestamp_ms = int(timestamp)
    interval_ms = timeframe_ms(timeframe)
    if timestamp_ms % interval_ms != 0:
        raise ValueError("OHLCV timestamp must align to the configured timeframe boundary")
    if previous_timestamp_ms is not None:
        expected = previous_timestamp_ms + interval_ms
        if timestamp_ms != expected:
            raise ValueError(
                "OHLCV continuity violation: "
                f"expected timestamp {expected}, received {timestamp_ms}"
            )
    return values


def _validate_live_config(config: LiveEngineConfig) -> None:
    if not isinstance(config.symbol, str) or not config.symbol.strip():
        raise ValueError("live engine symbol must be non-empty")
    timeframe_ms(config.timeframe)
    if isinstance(config.initial_capital, bool):
        raise ValueError("live engine initial_capital must be positive and finite")
    try:
        initial_capital = float(config.initial_capital)
    except (TypeError, ValueError) as exc:
        raise ValueError("live engine initial_capital must be positive and finite") from exc
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("live engine initial_capital must be positive and finite")
    if (
        isinstance(config.max_window, bool)
        or not isinstance(config.max_window, int)
        or config.max_window <= 0
    ):
        raise ValueError("live engine max_window must be a positive integer")


def _validate_live_setup(setup: TradeSetup, *, expected_direction: str) -> None:
    if setup.direction != expected_direction:
        raise ValueError("invalid live trade setup: direction does not match the strategy signal")
    if setup.entry_type not in {"market", "limit", "stop"}:
        raise ValueError("invalid live trade setup: unsupported entry type")
    try:
        entry_price = float(setup.entry_price)
        stop_loss = float(setup.stop_loss)
        take_profit = float(setup.take_profit)
        size = float(setup.size)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "invalid live trade setup: entry, stop, target, and size are required"
        ) from exc
    if any(
        not math.isfinite(value) or value <= 0
        for value in (entry_price, stop_loss, take_profit, size)
    ):
        raise ValueError(
            "invalid live trade setup: entry, stop, target, and size must be positive and finite"
        )
    if expected_direction == "long" and not stop_loss < entry_price < take_profit:
        raise ValueError(
            "invalid live trade setup: long protection must satisfy stop < entry < target"
        )
    if expected_direction == "short" and not take_profit < entry_price < stop_loss:
        raise ValueError(
            "invalid live trade setup: short protection must satisfy target < entry < stop"
        )


def _validate_fill_against_protection(setup: TradeSetup, fill: Fill) -> None:
    try:
        fill_price = float(fill.price)
        fill_quantity = float(fill.quantity)
        stop_loss = float(setup.stop_loss)
        take_profit = float(setup.take_profit)
    except (TypeError, ValueError) as exc:
        raise ValueError("entry fill is invalid or outside configured protection") from exc
    expected_side = "buy" if setup.direction == "long" else "sell"
    if (
        fill.side != expected_side
        or any(
            not math.isfinite(value) or value <= 0
            for value in (fill_price, fill_quantity, stop_loss, take_profit)
        )
        or (fill.side == "buy" and not stop_loss < fill_price < take_profit)
        or (fill.side == "sell" and not take_profit < fill_price < stop_loss)
    ):
        raise ValueError("entry fill is invalid or outside configured protection")


@dataclass
class _Window:
    max_len: int
    ts: list[int] = field(default_factory=list)
    o: list[float] = field(default_factory=list)
    h: list[float] = field(default_factory=list)
    low: list[float] = field(default_factory=list)
    c: list[float] = field(default_factory=list)
    v: list[float] = field(default_factory=list)

    def append(self, row: np.ndarray) -> None:
        self.ts.append(int(row[0]))
        self.o.append(float(row[1]))
        self.h.append(float(row[2]))
        self.low.append(float(row[3]))
        self.c.append(float(row[4]))
        self.v.append(float(row[5]))
        if len(self.c) > self.max_len:
            for lst in (self.ts, self.o, self.h, self.low, self.c, self.v):
                del lst[0]


class LiveEngine:
    def __init__(
        self,
        graph: dict,
        config: LiveEngineConfig,
        *,
        on_event: Callable[[dict], None] = _noop,
        on_trade: Callable[[dict], None] = _noop,
        on_bar: Callable[[dict], None] = _noop,
        on_status: Callable[[dict], None] = _noop,
        session_id: str = "",
        broker: Broker | None = None,
        on_order_intent: Callable[[dict], None] = _noop,
        on_order_ack: Callable[[dict], None] = _noop,
        on_fill: Callable[[dict], None] = _noop,
        on_audit: Callable[[dict], None] = _noop,
        on_incident: Callable[[dict], None] = _noop,
    ) -> None:
        _validate_live_config(config)
        self._strategy = assemble_from_graph(graph)
        self._strategy.config = {"symbol": config.symbol}
        self._config = config
        self._session_id = session_id or uuid.uuid4().hex
        self._broker = broker or PaperBroker(config.initial_capital)
        self._account = PlatformAccountState(starting_balance=config.initial_capital)
        self._window = _Window(max_len=config.max_window)
        if config.history is not None:
            try:
                history = np.asarray(config.history, dtype=np.float64)
            except (TypeError, ValueError) as exc:
                raise ValueError("OHLCV history must have shape (N, 6)") from exc
            if history.ndim != 2:
                raise ValueError("OHLCV history must have shape (N, 6)")
            previous_timestamp_ms: int | None = None
            for row in history:
                validated = _validate_ohlcv_row(
                    row,
                    previous_timestamp_ms=previous_timestamp_ms,
                    timeframe=config.timeframe,
                )
                self._window.append(validated)
                previous_timestamp_ms = int(validated[0])
        self._on_event = on_event
        self._on_trade = on_trade
        self._on_bar = on_bar
        self._on_status = on_status
        self._on_order_intent = on_order_intent
        self._on_order_ack = on_order_ack
        self._on_fill = on_fill
        self._on_audit = on_audit
        self._on_incident = on_incident
        self._bar_index = len(self._window.c)
        self._trade_id = 0
        self._pending_setup: TradeSetup | None = None
        self._open_setup: TradeSetup | None = None
        self._open_entry_ts: int = 0
        self._last_entry_client_order_id = ""
        self._entries_halted = False
        self._broker_entries_halted = False
        self._external_entry_pending = False
        self._containment_attempted = False
        self._containment_in_progress = False
        self._containment_confirmed: bool | None = None

    def run(self, feed: LiveFeed, stop: StopSignal) -> None:
        self._emit(EventType.SESSION_START, {"symbol": self._config.symbol})
        try:
            try:
                for row in feed.bars(stop):
                    self._process_bar(row)
            except Exception:
                if self._broker.target != "paper" and self._containment_confirmed is not True:
                    self._contain_exposure("session_error")
                try:
                    self._finalize(require_confirmation=False)
                except Exception as cleanup_exc:
                    self._on_incident(
                        {
                            "session_id": self._session_id,
                            "type": "finalization_failed",
                            "payload": {
                                "error": safe_exception_message(cleanup_exc),
                            },
                        }
                    )
                raise
            else:
                self._finalize()
        finally:
            self._emit(EventType.SESSION_END, {})

    def _process_bar(self, row: object) -> None:
        row = _validate_ohlcv_row(
            row,
            previous_timestamp_ms=self._window.ts[-1] if self._window.ts else None,
            timeframe=self._config.timeframe,
        )
        ts, o, h, low, c, v = (int(row[0]), *(float(x) for x in row[1:6]))
        self._bar_index += 1
        self._window.append(row)
        process_bar = getattr(self._broker, "process_bar", None)
        paper_fills = (
            process_bar(ts_ms=ts, open=o, high=h, low=low, close=c)
            if process_bar is not None
            else []
        )
        for fill in paper_fills:
            if fill.kind == "entry":
                self._on_open(fill)
            else:
                self._on_close(fill)
        self._poll_broker_fills()
        self._account.on_bar(equity=self._broker_equity(), timestamp_ms=ts)
        self._inject_state(ts, o, h, low, c, v)
        on_bar = getattr(self._strategy, "on_bar", None)
        if on_bar is not None:
            on_bar()
        entry_canceled = False
        if (
            self._broker_position() is None
            and self._broker_pending()
            and getattr(self._strategy, "should_cancel_entry", lambda: False)()
        ):
            if not self._cancel_working_orders():
                self._contain_exposure("entry_cancel_failed")
                raise RuntimeError("working entry order could not be canceled")
            self._external_entry_pending = False
            self._pending_setup = None
            entry_canceled = True
        if (
            not entry_canceled
            and self._broker_position() is None
            and not self._broker_pending()
            and not self._broker_entries_halted
        ):
            self._try_enter(ts, c)
        elif self._broker_position() is not None:
            new_sl = self._strategy.on_sl_update(self._trade_id)
            if new_sl is not None:
                modify_stop = getattr(self._broker, "modify_stop", None)
                if modify_stop is None:
                    self._contain_exposure("dynamic_stop_unsupported")
                    raise RuntimeError("broker cannot apply a dynamic protection update")
                try:
                    ack = modify_stop(new_sl)
                except Exception:
                    self._contain_exposure("dynamic_stop_failed")
                    raise
                if ack is not None and ack.status not in {"accepted", "filled"}:
                    self._contain_exposure("dynamic_stop_rejected")
                    raise RuntimeError("broker rejected the dynamic protection update")
        self._emit_bar(ts, o, h, low, c)
        self._emit_status()

    def _try_enter(self, ts: int, close: float) -> None:
        direction = None
        if self._strategy.should_long():
            direction = "long"
        elif self._strategy.should_short():
            direction = "short"
        self._entries_halted = self._strategy.was_blocked()
        if direction is None:
            return
        if not self._strategy._execute_filters():  # noqa: SLF001 - own brain, mirrors bt_adapter
            self._emit(EventType.FILTER_REJECTED, {"direction": direction})
            return
        setup = self._strategy.go_long() if direction == "long" else self._strategy.go_short()
        _validate_live_setup(setup, expected_direction=direction)
        side = "buy" if direction == "long" else "sell"
        intent_id = f"entry-{self._bar_index}-{self._trade_id + 1}"
        client_order_id = make_client_order_id(
            self._session_id, intent_id, self._broker.target, "entry"
        )
        intent = BrokerOrderIntent(
            session_id=self._session_id,
            intent_id=intent_id,
            client_order_id=client_order_id,
            symbol=self._config.symbol,
            side=side,
            order_type=setup.entry_type,
            quantity=str(setup.size),
            price=str(setup.entry_price),
            stop_price=str(setup.stop_loss),
            target_price=str(setup.take_profit),
            target=self._broker.target,
        )
        self._on_order_intent(_intent_to_dict(intent))
        self._pending_setup = setup
        self._external_entry_pending = True
        self._last_entry_client_order_id = intent.client_order_id
        try:
            ack = self._broker.submit_entry(intent)
        except Exception:
            self._contain_exposure("entry_state_unknown")
            raise
        self._on_order_ack({**_ack_to_dict(ack), "intent_id": intent.intent_id})
        if ack.status not in {"accepted", "filled"}:
            self._external_entry_pending = False
            self._pending_setup = None
            self._broker_entries_halted = True
            self._entries_halted = True
            self._on_incident(
                {
                    "session_id": self._session_id,
                    "type": "entry_rejected",
                    "payload": {"ack": _ack_to_dict(ack), "intent_id": intent.intent_id},
                }
            )
            return
        self._emit(
            EventType.SIGNAL_DETECTED,
            {
                "direction": setup.direction,
                "entry_price": setup.entry_price,
                "stop_loss": setup.stop_loss,
                "take_profit": setup.take_profit,
                "why_entry": list(setup.why_entry),
            },
        )
        self._emit(
            EventType.ORDER_PLACED,
            {"direction": setup.direction, "entry_type": setup.entry_type, "size": setup.size},
        )
        if self._broker.target != "paper":
            try:
                self._poll_broker_fills()
            except Exception:
                self._contain_exposure("immediate_fill_processing_failed")
                raise
            if ack.status == "filled" and self._account.snapshot().open_position is None:
                self._contain_exposure("filled_entry_state_missing")
                raise RuntimeError("filled entry was not available for immediate protection")
        fill_market = getattr(self._broker, "fill_market_if_pending", None)
        fill = fill_market(ts_ms=ts, price=close) if fill_market is not None else None
        if fill is not None:
            self._on_open(fill)
            self._poll_broker_fills()

    def _poll_broker_fills(self) -> None:
        for fill in self._broker.poll_fills(self._session_id):
            self._on_fill(_broker_fill_to_dict(fill))
            if self._broker.target != "paper":
                self._process_external_fill(fill)

    def _on_open(self, fill: Fill) -> None:
        self._external_entry_pending = False
        self._trade_id += 1
        setup = self._pending_setup
        self._open_setup = setup
        self._open_entry_ts = fill.timestamp_ms
        self._pending_setup = None
        if setup is not None:
            try:
                _validate_fill_against_protection(setup, fill)
            except ValueError:
                self._entries_halted = True
                self._broker_entries_halted = True
                self._on_incident(
                    {
                        "session_id": self._session_id,
                        "type": "protection_unconfirmed",
                        "payload": {"reason": "fill_outside_configured_protection"},
                    }
                )
                if self._broker.target == "paper":
                    self._broker.flatten(ts_ms=fill.timestamp_ms, price=fill.price)
                    self._poll_broker_fills()
                else:
                    self._contain_exposure("fill_outside_configured_protection")
                raise
            self._strategy.on_open_position(self._trade_id, setup)
            self._account.on_open(
                side=fill.side,
                entry_price=fill.price,
                quantity=fill.quantity,
                current_stop=setup.stop_loss,
                margin=0.0,
            )
            self._place_protection(setup, fill)
        self._emit(
            EventType.TRADE_OPENED,
            {
                "direction": "long" if fill.side == "buy" else "short",
                "entry_price": fill.price,
                "size": fill.quantity,
                "why_entry": list(setup.why_entry) if setup else [],
            },
        )

    def _process_external_fill(self, fill: BrokerFill) -> None:
        if fill.role == "entry":
            if fill.status in {"canceled", "expired"}:
                self._external_entry_pending = False
                if _positive_float(fill.quantity) is not None:
                    self._on_incident(
                        {
                            "session_id": self._session_id,
                            "type": "terminal_entry_fill",
                            "payload": _broker_fill_to_dict(fill),
                        }
                    )
                    self._contain_exposure("terminal_entry_fill")
                    raise RuntimeError("terminal entry fill reported additional executed quantity")
                return
            if fill.status not in {"partial", "filled"}:
                return
            if self._account.snapshot().open_position is not None:
                self._on_incident(
                    {
                        "session_id": self._session_id,
                        "type": "unexpected_additional_entry_fill",
                        "payload": _broker_fill_to_dict(fill),
                    }
                )
                self._contain_exposure("unexpected_additional_entry_fill")
                raise RuntimeError("additional entry fill arrived after position protection")
            if fill.status == "partial":
                self._on_incident(
                    {
                        "session_id": self._session_id,
                        "type": "partial_entry_fill",
                        "payload": _broker_fill_to_dict(fill),
                    }
                )
                self._contain_exposure("partial_entry_fill")
                raise RuntimeError("partial entry fill requires confirmed containment")
            synthetic = _broker_fill_to_paper_fill(fill)
            if synthetic is not None:
                self._on_open(synthetic)
            return
        if fill.role not in {"stop", "target", "flatten"}:
            return
        if fill.status in {"canceled", "expired"}:
            incident_type = (
                "protection_lost" if fill.role in {"stop", "target"} else "flatten_unconfirmed"
            )
            self._on_incident(
                {
                    "session_id": self._session_id,
                    "type": incident_type,
                    "payload": _broker_fill_to_dict(fill),
                }
            )
            self._contain_exposure(incident_type)
            raise RuntimeError(f"{fill.role} protective order became {fill.status}")
        if fill.status not in {"partial", "filled"}:
            return
        if fill.status == "partial":
            self._contain_exposure("partial_protective_fill")
            raise RuntimeError("partial protective fill requires reconciliation")
        synthetic = self._external_close_fill(fill)
        if synthetic is None:
            self._contain_exposure("invalid_protective_fill")
            raise RuntimeError("protective fill omitted price or quantity")
        self._cancel_working_orders()
        if fill.role == "flatten" and self._account.snapshot().open_position is None:
            self._pending_setup = None
            self._open_setup = None
            return
        self._on_close(synthetic)

    def _external_close_fill(self, fill: BrokerFill) -> Fill | None:
        if fill.quantity is None or fill.price is None:
            return None
        position = self._account.snapshot().open_position
        quantity = float(fill.quantity)
        price = float(fill.price)
        realized_pnl = fill.realized_pnl
        if realized_pnl is None and position is not None:
            sign = 1.0 if position.side == "buy" else -1.0
            realized_pnl = str((price - position.entry_price) * quantity * sign)
        kind = {
            "stop": "stop_loss",
            "target": "take_profit",
            "flatten": "manual_close",
        }[fill.role]
        return Fill(
            kind=kind,
            side=fill.side,
            price=price,
            quantity=quantity,
            timestamp_ms=fill.timestamp_ms,
            realized_pnl=float(realized_pnl or 0.0),
        )

    def _place_protection(self, setup: TradeSetup, fill: Fill) -> None:
        stop_client_order_id = make_client_order_id(
            self._session_id,
            f"protect-{self._trade_id}",
            self._broker.target,
            "stop",
        )
        target_client_order_id = make_client_order_id(
            self._session_id,
            f"protect-{self._trade_id}",
            self._broker.target,
            "target",
        )
        intent = ProtectiveOrderIntent(
            session_id=self._session_id,
            entry_client_order_id=self._last_entry_client_order_id,
            stop_client_order_id=stop_client_order_id,
            target_client_order_id=target_client_order_id,
            symbol=self._config.symbol,
            side=fill.side,
            quantity=str(fill.quantity),
            stop_price=str(setup.stop_loss),
            target_price=str(setup.take_profit),
            target=self._broker.target,
        )
        self._on_audit(
            {
                "session_id": self._session_id,
                "type": "protection_intent",
                "payload": {
                    "entry_client_order_id": intent.entry_client_order_id,
                    "stop_client_order_id": intent.stop_client_order_id,
                    "target_client_order_id": intent.target_client_order_id,
                    "target": intent.target,
                },
            }
        )
        try:
            acks = self._broker.place_protection(intent)
        except Exception:
            self._entries_halted = True
            self._broker_entries_halted = True
            self._on_incident(
                {
                    "session_id": self._session_id,
                    "type": "protection_unconfirmed",
                    "payload": {"acks": [], "reason": "submission_failed"},
                }
            )
            self._contain_exposure("protection_submission_failed")
            raise
        expected_client_order_ids = {
            intent.stop_client_order_id,
            intent.target_client_order_id,
        }
        confirmed_client_order_ids = {
            ack.client_order_id for ack in acks if ack.status in {"accepted", "filled"}
        }
        if (
            len(acks) != len(expected_client_order_ids)
            or confirmed_client_order_ids != expected_client_order_ids
        ):
            self._entries_halted = True
            self._broker_entries_halted = True
            self._on_incident(
                {
                    "session_id": self._session_id,
                    "type": "protection_unconfirmed",
                    "payload": {"acks": [_ack_to_dict(ack) for ack in acks]},
                }
            )
            self._contain_exposure("protection_unconfirmed")
            raise RuntimeError("protection could not be confirmed")
        self._on_audit(
            {
                "session_id": self._session_id,
                "type": "protection_confirmed",
                "payload": {"acks": [_ack_to_dict(ack) for ack in acks]},
            }
        )

    def _on_close(self, fill: Fill) -> None:
        self._strategy.on_close_position(self._trade_id, {"pnl": fill.realized_pnl})
        self._account.on_close(realized_pnl=fill.realized_pnl)
        record = self._trade_record(self._open_setup, fill)
        self._open_setup = None
        self._emit(
            EventType.TRADE_CLOSED,
            {
                "pnl": fill.realized_pnl,
                "exit_price": fill.price,
                "exit_reason": record["reason"],
                "entry_price": record["entry_price"],
                "size": record["size"],
            },
        )
        self._on_trade(record)

    def _trade_record(self, setup: TradeSetup | None, fill: Fill) -> dict:
        direction = "long" if fill.side == "buy" else "short"
        reason = {"stop_loss": "sl", "take_profit": "tp", "manual_close": "manual"}.get(
            fill.kind, "other"
        )
        entry_price = setup.entry_price if setup else fill.price
        return {
            "entry_time": _iso(self._open_entry_ts),
            "exit_time": _iso(fill.timestamp_ms),
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": fill.price,
            "size": fill.quantity,
            "pnl": fill.realized_pnl,
            "pnl_pct": (fill.realized_pnl / (entry_price * fill.quantity) * 100.0)
            if entry_price and fill.quantity
            else 0.0,
            "reason": reason,
        }

    def _finalize(self, *, require_confirmation: bool = True) -> None:
        position = self._broker_position()
        if self._broker.target != "paper":
            if (
                self._broker_pending()
                or position is not None
                or (self._containment_attempted and self._containment_confirmed is not True)
            ):
                self._contain_exposure("session_finalize", retry=True)
            self._emit_status(terminal=True)
            if require_confirmation and self._containment_confirmed is False:
                raise RuntimeError("sandbox containment could not be confirmed")
            return

        if self._broker_pending():
            self._cancel_working_orders()
            self._external_entry_pending = False
            self._pending_setup = None
        if position is not None:
            ts = self._window.ts[-1] if self._window.ts else 0
            price = self._window.c[-1] if self._window.c else 0.0
            fill = self._broker.flatten(ts_ms=ts, price=price)
            if fill is not None:
                self._on_close(fill)
        self._poll_broker_fills()
        self._emit_status(terminal=True)

    def _cancel_working_orders(self) -> bool:
        broker_pending_before = bool(getattr(self._broker, "pending", False))
        engine_pending_before = self._external_entry_pending
        protected_position_before = (
            self._broker.target != "paper" and self._account.snapshot().open_position is not None
        )
        try:
            acks = self._broker.cancel_all(self._config.symbol, self._session_id)
            for ack in acks:
                self._on_order_ack(_ack_to_dict(ack))
        except Exception as exc:
            self._on_incident(
                {
                    "session_id": self._session_id,
                    "type": "cancel_failed",
                    "payload": {"error": safe_exception_message(exc)},
                }
            )
            return False
        if any(ack.status not in {"canceled", "filled"} for ack in acks):
            return False
        if bool(getattr(self._broker, "pending", False)):
            return False
        if (
            (engine_pending_before and not broker_pending_before) or protected_position_before
        ) and not acks:
            return False
        return True

    def _contain_exposure(self, reason: str, *, retry: bool = False) -> None:
        if (
            self._containment_confirmed is True
            or self._containment_in_progress
            or (self._containment_attempted and not retry)
        ):
            return
        self._containment_attempted = True
        self._containment_in_progress = True
        self._entries_halted = True
        self._broker_entries_halted = True
        try:
            cancel_confirmed = self._cancel_working_orders()
            self._external_entry_pending = False
            flatten_confirmed = False
            try:
                ack = self._broker.flatten(self._config.symbol, self._session_id)
                if isinstance(ack, BrokerOrderAck):
                    self._on_order_ack(_ack_to_dict(ack))
                    flatten_confirmed = ack.status == "filled"
                close_fill_confirmed = self._drain_containment_fills()
                flat_after_drain = self._broker_position() is None
                if ack is None:
                    flatten_confirmed = flat_after_drain
                elif close_fill_confirmed:
                    flatten_confirmed = True
            except Exception as exc:
                self._on_incident(
                    {
                        "session_id": self._session_id,
                        "type": "flatten_failed",
                        "payload": {
                            "reason": reason,
                            "error": safe_exception_message(exc),
                        },
                    }
                )
            flat = self._broker_position() is None
            self._containment_confirmed = cancel_confirmed and flatten_confirmed and flat
            if not self._containment_confirmed:
                self._on_incident(
                    {
                        "session_id": self._session_id,
                        "type": "containment_unconfirmed",
                        "payload": {
                            "reason": reason,
                            "cancel_confirmed": cancel_confirmed,
                            "flatten_confirmed": flatten_confirmed,
                            "flat": flat,
                        },
                    }
                )
        finally:
            self._containment_in_progress = False

    def _drain_containment_fills(self) -> bool:
        close_fill_confirmed = False
        for fill in self._broker.poll_fills(self._session_id):
            self._on_fill(_broker_fill_to_dict(fill))
            if fill.role not in {"stop", "target", "flatten"} or fill.status != "filled":
                continue
            synthetic = self._external_close_fill(fill)
            if synthetic is None:
                continue
            close_fill_confirmed = True
            if self._account.snapshot().open_position is not None:
                self._on_close(synthetic)
        return close_fill_confirmed

    def _inject_state(self, ts, o, h, low, c, v) -> None:
        s = self._strategy
        s.close, s.high, s.low, s.open, s.volume = c, h, low, o, v
        s.bar_index = self._bar_index
        s.timestamp_ms = ts
        s.closes = np.array(self._window.c, dtype=float)
        s.highs = np.array(self._window.h, dtype=float)
        s.lows = np.array(self._window.low, dtype=float)
        s.opens = np.array(self._window.o, dtype=float)
        s.volumes = np.array(self._window.v, dtype=float)
        s.htf_closes = s.htf_highs = s.htf_lows = s.htf_opens = s.htf_volumes = None
        s.account_value = self._broker_equity()
        pos = self._broker_position()
        s.position_size = float(pos.quantity) if pos else 0.0
        s.position_direction = None if pos is None else ("long" if pos.side == "buy" else "short")

    def _emit(self, event_type: EventType, payload: dict) -> None:
        ts = self._window.ts[-1] if self._window.ts else 0
        ev = EngineEvent(
            event_type=event_type,
            bar_index=self._bar_index,
            timestamp_ms=ts,
            payload=payload,
        )
        self._on_event(
            {
                "event_type": ev.event_type.value,
                "bar_index": ev.bar_index,
                "timestamp_ms": ev.timestamp_ms,
                "payload": ev.payload,
            }
        )

    def _emit_bar(self, ts, o, h, low, c) -> None:
        self._on_bar(
            {
                "ts": ts,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "equity": self._broker_equity(),
                "unrealized_pnl": self._broker_equity() - self._broker_balance(),
            }
        )

    def _emit_status(self, *, terminal: bool = False) -> None:
        snap = self._account.snapshot()
        pos = self._broker_position()
        self._on_status(
            {
                "entries_halted": self._entries_halted,
                "metrics": {
                    "equity": snap.equity,
                    "balance": snap.balance,
                    "realized_pnl": snap.realized_pnl,
                    "unrealized_pnl": snap.unrealized_pnl,
                    "daily_pnl": snap.daily_pnl,
                    "drawdown_pct": snap.drawdown_pct,
                },
                "open_position": None
                if pos is None
                else {
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "quantity": pos.quantity,
                    "current_stop": getattr(pos, "stop_price", getattr(pos, "current_stop", None)),
                    "take_profit": getattr(
                        pos,
                        "target_price",
                        self._open_setup.take_profit if self._open_setup is not None else None,
                    ),
                },
                "bars_processed": self._bar_index,
                "containment_attempted": self._containment_attempted,
                "containment_confirmed": self._containment_confirmed,
                "terminal": terminal,
            }
        )

    def _broker_equity(self) -> float:
        if self._broker.target == "paper":
            return float(self._broker.equity)
        snap = self._account.snapshot()
        position = snap.open_position
        if position is None or not self._window.c:
            return snap.balance
        close = self._window.c[-1]
        sign = 1.0 if position.side == "buy" else -1.0
        return snap.balance + (close - position.entry_price) * position.quantity * sign

    def _broker_balance(self) -> float:
        if self._broker.target == "paper":
            return float(self._broker.balance)
        return self._account.snapshot().balance

    def _broker_position(self):
        broker_position = getattr(self._broker, "position", None)
        return broker_position or self._account.snapshot().open_position

    def _broker_pending(self) -> bool:
        return self._external_entry_pending or bool(getattr(self._broker, "pending", False))


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed
