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
from copy import copy, deepcopy
from dataclasses import asdict, dataclass, field, replace

import numpy as np

from koval._version import __version__
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
from koval.engine.event_timeline import CanonicalEvent, CanonicalEventTimeline
from koval.engine.execution_evidence import ExecutionEvidenceUpdate
from koval.engine.execution_proxy import ExecutionProxyConfig
from koval.engine.fee_evidence import FeeScheduleEvidence
from koval.engine.funding import FundingSeries
from koval.engine.higher_timeframe import confirmed_higher_timeframe_bars
from koval.engine.history_window import DEFAULT_HISTORY_BARS
from koval.engine.instrument_risk import InstrumentSpecEvidence, MarkPriceSeries
from koval.engine.live_feed import LiveFeed, StopSignal
from koval.engine.market_identity import resolve_market_identity
from koval.engine.paper_broker import Fill, PaperBroker
from koval.engine.paper_profile import resolve_paper_profile
from koval.engine.run_boundaries import resolve_runtime_boundaries
from koval.engine.run_identity import (
    CandleStreamIdentity,
    build_run_identity,
    content_sha256,
    execution_evidence_manifest,
)
from koval.engine.runtime_journal import RuntimeJournal
from koval.exchanges.auth import safe_exception_message
from koval.exchanges.base import timeframe_ms
from koval.exchanges.execution_compatibility import compatibility_for
from koval.exchanges.markets import canonical_market
from koval.strategy.base.trade_setup import TradeSetup
from koval.strategy.block_assembler import assemble_from_graph

_CANONICAL_EVENT_CONTENT_FIELDS = (
    "event_id",
    "kind",
    "timestamp_ms",
    "source",
    "source_sequence",
    "payload",
)


def _canonical_event_digest(record: dict) -> str:
    """Hash event identity/content without timeline-local batch placement."""
    return content_sha256({field: record[field] for field in _CANONICAL_EVENT_CONTENT_FIELDS})


@dataclass
class LiveEngineConfig:
    symbol: str
    timeframe: str
    initial_capital: float
    history: np.ndarray | None = None
    max_window: int = DEFAULT_HISTORY_BARS
    execution: dict | None = None
    higher_timeframe: str | None = None
    requires_higher_timeframe: bool = False
    market: str = "future"
    funding: FundingSeries | None = None
    fee_schedule: FeeScheduleEvidence | None = None
    instrument_specs: tuple[InstrumentSpecEvidence, ...] = ()
    mark_prices: MarkPriceSeries | None = None
    execution_proxy: ExecutionProxyConfig | None = None
    daily_baseline_equity: float | None = None
    peak_equity: float | None = None
    exchange: str | None = None
    end_of_data_policy: str = "flatten_at_last_close"
    runtime_contract: dict | None = None
    observed_quote_execution: bool = False


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


def _venue_commission(fill: BrokerFill) -> float:
    """The quote-asset commission a venue reported, or 0 when it is unconverted."""
    if fill.metadata.get("commission_unconverted"):
        return 0.0
    try:
        return float(fill.metadata.get("commission") or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
        commission=_venue_commission(fill),
        reference_price=float(fill.price),
    )


def _trade_setup_to_dict(setup: TradeSetup | object | None) -> dict | None:
    """Normalize strategy-compatible setup objects into the public dataclass."""
    if setup is None:
        return None
    normalized = TradeSetup(
        direction=setup.direction,
        entry_price=float(setup.entry_price),
        stop_loss=float(setup.stop_loss),
        take_profit=getattr(setup, "take_profit", None),
        size=getattr(setup, "size", None),
        entry_type=getattr(setup, "entry_type", "limit"),
        why_entry=list(getattr(setup, "why_entry", ())),
        indicators_at_entry=dict(getattr(setup, "indicators_at_entry", {})),
        sl_calc_expr=getattr(setup, "sl_calc_expr", None),
        tp_calc_expr=getattr(setup, "tp_calc_expr", None),
        annotations=list(getattr(setup, "annotations", ())),
    )
    return asdict(normalized)


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
    if config.end_of_data_policy not in {"flatten_at_last_close", "mark_at_last_close"}:
        raise ValueError("unsupported end_of_data_policy")
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
    if config.higher_timeframe is not None:
        primary_ms = timeframe_ms(config.timeframe)
        higher_ms = timeframe_ms(config.higher_timeframe)
        if higher_ms <= primary_ms or higher_ms % primary_ms:
            raise ValueError(
                "live engine higher_timeframe must be a larger integer multiple of timeframe"
            )
    if config.requires_higher_timeframe and config.higher_timeframe is None:
        raise ValueError("live engine graph requires a configured higher_timeframe")


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


def _validate_fill_against_protection(
    setup: TradeSetup, fill: Fill, *, paper_matched: bool = False
) -> None:
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
        or (not paper_matched and fill.side == "buy" and not stop_loss < fill_price < take_profit)
        or (not paper_matched and fill.side == "sell" and not take_profit < fill_price < stop_loss)
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

    def rows(self) -> list[list[float]]:
        return [
            [float(ts), o, h, low, c, v]
            for ts, o, h, low, c, v in zip(
                self.ts, self.o, self.h, self.low, self.c, self.v, strict=True
            )
        ]


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
        on_record: Callable[[dict], None] | None = None,
        on_checkpoint: Callable[[dict], None] = _noop,
    ) -> None:
        self._boundaries = resolve_runtime_boundaries(config.runtime_contract)
        if self._boundaries is not None:
            self._boundaries.validate_inputs(
                timeframe=config.timeframe, initial_capital=config.initial_capital
            )
            for name in ("daily_baseline_equity", "peak_equity"):
                value = getattr(config, name)
                if value is not None and value != getattr(self._boundaries, name):
                    raise ValueError(f"runtime contract conflicts with {name}")
            config = replace(
                config,
                daily_baseline_equity=self._boundaries.daily_baseline_equity,
                peak_equity=self._boundaries.peak_equity,
                end_of_data_policy=self._boundaries.end_of_data_policy,
                runtime_contract=self._boundaries.as_dict(),
            )
        _validate_live_config(config)
        self._strategy = assemble_from_graph(graph)
        self._strategy.config = {"symbol": config.symbol}
        self._config = config
        self._terminal_policy: str | None = None
        self._session_id = session_id or uuid.uuid4().hex
        self._journal = RuntimeJournal(self._session_id, on_record)
        self._on_checkpoint = on_checkpoint
        self._last_input: np.ndarray | None = None
        self._ledger_cursor = 0
        self._cashflow_fill_ids: dict[int, str] = {}
        self._canonical_event_hashes: dict[str, str] = {}
        self._canonical_source_sequences: dict[str, int] = {}
        self._decision_id: str | None = None
        self._decision_context: dict | None = None
        self._open_decision_context: dict | None = None
        self._pending_decision_context: dict | None = None
        self._profile = resolve_paper_profile(config.execution)
        self._broker = broker or PaperBroker(
            config.initial_capital,
            profile=self._profile,
            market=config.market,
            funding=config.funding,
            fee_schedule=config.fee_schedule,
            instrument_specs=config.instrument_specs,
            mark_prices=config.mark_prices,
            execution_proxy=config.execution_proxy,
            exchange=config.exchange,
            observed_quote_execution=config.observed_quote_execution,
        )
        if config.observed_quote_execution and (
            not isinstance(self._broker, PaperBroker) or not self._broker.observed_quote_execution
        ):
            raise ValueError("observed quote execution requires a matching paper broker")
        self._validate_broker_config(config)
        if self._broker.target != "paper" and config.end_of_data_policy != "flatten_at_last_close":
            raise ValueError("mark_at_last_close is available only for paper replay")
        exchange = config.exchange or getattr(self._broker, "exchange", None)
        self._market_identity = (
            resolve_market_identity(exchange=exchange, market=config.market, symbol=config.symbol)
            if exchange is not None
            else None
        )
        if exchange is not None:
            compatibility_for(exchange, config.market, self._broker.target)
        self._strategy_sha256 = content_sha256(graph)
        self._primary_identity = CandleStreamIdentity(config.timeframe)
        self._warmup_identity = CandleStreamIdentity(config.timeframe)
        broker_ledger = (
            getattr(self._broker, "ledger", None) if self._broker.target == "paper" else None
        )
        self._uses_broker_ledger = broker_ledger is not None
        self._account = PlatformAccountState(
            starting_balance=config.initial_capital,
            daily_baseline_equity=config.daily_baseline_equity,
            peak_equity=config.peak_equity,
            ledger=broker_ledger,
        )
        bind_account = getattr(self._strategy, "bind_account", None)
        if bind_account is not None:
            bind_account(self._account.snapshot)
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
                if self._boundaries is not None:
                    expected = (
                        previous_timestamp_ms + timeframe_ms(config.timeframe)
                        if previous_timestamp_ms is not None
                        else self._boundaries.warmup_start_ms
                    )
                    if (
                        validated[0] != expected
                        or validated[0] >= self._boundaries.evaluation_start_ms
                    ):
                        raise ValueError("runtime warmup coverage does not match history")
                self._accept_input(validated, phase="warmup")
                self._window.append(validated)
                self._journal.write("bar_processed", int(validated[0]), {"phase": "warmup"})
                previous_timestamp_ms = int(validated[0])
            warmup = history if self._boundaries is not None else history[-config.max_window :]
            for row in warmup:
                self._warmup_identity.append(row)
        self._on_event = self._record_callback("event", on_event)
        self._on_trade = self._record_callback("trade", on_trade)
        self._on_bar = on_bar
        self._on_status = on_status
        self._on_order_intent = self._record_callback("order_intent", on_order_intent)
        self._on_order_ack = self._record_callback("order_ack", on_order_ack)
        self._on_fill = self._record_callback("fill", on_fill)
        self._on_audit = self._record_callback("audit", on_audit)
        self._on_incident = self._record_callback("incident", on_incident)
        self._bar_index = (
            self._warmup_identity.as_dict()["row_count"]
            if self._boundaries
            else len(self._window.c)
        )
        self._trade_id = 0
        self._pending_setup: TradeSetup | None = None
        self._open_setup: TradeSetup | None = None
        self._open_entry_ts: int = 0
        self._open_entry_fill: Fill | None = None
        self._partial_exit_realized_pnl = 0.0
        self._partial_exit_commission = 0.0
        self._partial_exit_quantity = 0.0
        self._partial_exit_notional = 0.0
        self._partial_exit_spread_cost = 0.0
        self._partial_exit_slippage_cost = 0.0
        self._higher_timeframe_available = False
        self._last_entry_client_order_id = ""
        self._last_order_received_at_ms: int | None = None
        self._last_protection_received_at_ms: int | None = None
        self._entries_halted = False
        self._broker_entries_halted = False
        self._external_entry_pending = False
        self._containment_attempted = False
        self._containment_in_progress = False
        self._containment_confirmed: bool | None = None

    @property
    def checkpoint(self) -> dict:
        """Last acknowledged journal plus broker and strategy recovery state."""
        strategy_checkpoint = getattr(self._strategy, "runtime_checkpoint", None)
        broker_checkpoint = getattr(self._broker, "checkpoint", None)
        return {
            **self._journal.checkpoint,
            "bar_index": self._bar_index,
            "strategy": (
                strategy_checkpoint()
                if strategy_checkpoint is not None
                else {
                    "version": "koval_strategy_checkpoint_v1",
                    "kind": "unsupported_custom_strategy",
                }
            ),
            "broker": (
                broker_checkpoint()
                if broker_checkpoint is not None
                else {
                    "version": "koval_broker_checkpoint_unavailable_v1",
                    "target": self._broker.target,
                }
            ),
            "engine_state": self._engine_checkpoint_state(),
        }

    @property
    def paper_quote_required(self) -> bool:
        """Whether a live paper order or position needs the next public quote."""
        return self._config.observed_quote_execution and (
            self._broker_pending() or self._broker_position() is not None
        )

    def _engine_checkpoint_state(self) -> dict:
        state = {
            "version": "koval_live_engine_state_v1",
            "identity": {
                "session_id": self._session_id,
                "symbol": self._config.symbol,
                "timeframe": self._config.timeframe,
                "market": canonical_market(self._config.market),
                "exchange": self._config.exchange,
                "strategy_sha256": self._strategy_sha256,
            },
            "account": self._account.checkpoint(),
            "window": self._window.rows(),
            "primary_identity": self._primary_identity.as_dict(),
            "warmup_identity": self._warmup_identity.as_dict(),
            "bar_index": self._bar_index,
            "trade_id": self._trade_id,
            "ledger_cursor": self._ledger_cursor,
            "cashflow_fill_ids": [
                [sequence, fill_id] for sequence, fill_id in sorted(self._cashflow_fill_ids.items())
            ],
            "canonical_event_ids": sorted(self._canonical_event_hashes),
            "canonical_event_hashes": [
                [event_id, digest]
                for event_id, digest in sorted(self._canonical_event_hashes.items())
            ],
            "canonical_source_sequences": dict(sorted(self._canonical_source_sequences.items())),
            "decision_id": self._decision_id,
            "decision_context": deepcopy(self._decision_context),
            "open_decision_context": deepcopy(self._open_decision_context),
            "pending_decision_context": deepcopy(self._pending_decision_context),
            "pending_setup": (_trade_setup_to_dict(self._pending_setup)),
            "open_setup": _trade_setup_to_dict(self._open_setup),
            "open_entry_ts": self._open_entry_ts,
            "open_entry_fill": (
                None if self._open_entry_fill is None else asdict(self._open_entry_fill)
            ),
            "partial_exit_realized_pnl": self._partial_exit_realized_pnl,
            "partial_exit_commission": self._partial_exit_commission,
            "partial_exit_quantity": self._partial_exit_quantity,
            "partial_exit_notional": self._partial_exit_notional,
            "partial_exit_spread_cost": self._partial_exit_spread_cost,
            "partial_exit_slippage_cost": self._partial_exit_slippage_cost,
            "higher_timeframe_available": self._higher_timeframe_available,
            "last_entry_client_order_id": self._last_entry_client_order_id,
            "entries_halted": self._entries_halted,
            "broker_entries_halted": self._broker_entries_halted,
            "external_entry_pending": self._external_entry_pending,
            "containment_attempted": self._containment_attempted,
            "containment_in_progress": self._containment_in_progress,
            "containment_confirmed": self._containment_confirmed,
            "terminal_policy": self._terminal_policy,
        }
        if self._config.observed_quote_execution:
            state["observed_quote_execution"] = True
            state["last_order_received_at_ms"] = self._last_order_received_at_ms
            state["last_protection_received_at_ms"] = self._last_protection_received_at_ms
        return state

    def restore_checkpoint(self, checkpoint: dict, records) -> None:
        """Restore one paper session at an exact completed-bar journal boundary.

        The retained journal is replayed only to rebuild candle identities and the
        bounded strategy window.  Economic effects come from the verified broker,
        strategy, and account snapshots and are therefore never applied twice.
        """
        if not isinstance(self._broker, PaperBroker):
            raise ValueError("only paper sessions can restore a runtime checkpoint")
        retained = tuple(deepcopy(list(records)))
        if checkpoint.get("sequence") != len(retained):
            raise ValueError("journal head does not equal runtime checkpoint")
        self._journal.restore(checkpoint, retained)
        if checkpoint.get("last_accepted_bar_ms") != checkpoint.get("last_processed_bar_ms"):
            raise ValueError("runtime checkpoint is not a completed-bar boundary")

        state = checkpoint.get("engine_state")
        if not isinstance(state, dict) or state.get("version") != "koval_live_engine_state_v1":
            raise ValueError("unsupported live engine checkpoint state")
        expected_identity = {
            "session_id": self._session_id,
            "symbol": self._config.symbol,
            "timeframe": self._config.timeframe,
            "market": canonical_market(self._config.market),
            "exchange": self._config.exchange,
            "strategy_sha256": self._strategy_sha256,
        }
        if state.get("identity") != expected_identity:
            raise ValueError("live engine checkpoint identity mismatch")

        rebuilt_window = _Window(max_len=self._config.max_window)
        primary = CandleStreamIdentity(self._config.timeframe)
        warmup = CandleStreamIdentity(self._config.timeframe)
        last_input: np.ndarray | None = None
        for record in retained:
            if record.get("kind") != "bar_received":
                continue
            payload = record.get("payload") or {}
            row = _validate_ohlcv_row(
                payload.get("ohlcv"),
                previous_timestamp_ms=(None if last_input is None else int(last_input[0])),
                timeframe=self._config.timeframe,
            )
            phase = payload.get("phase")
            if phase == "warmup":
                warmup.append(row)
            elif phase == "evaluation":
                primary.append(row)
            else:
                raise ValueError("runtime journal bar phase is invalid")
            rebuilt_window.append(row)
            last_input = row.copy()

        if rebuilt_window.rows() != state.get("window"):
            raise ValueError("live engine checkpoint window does not match journal")
        if primary.as_dict() != state.get("primary_identity"):
            raise ValueError("live engine primary identity does not match journal")
        if warmup.as_dict() != state.get("warmup_identity"):
            raise ValueError("live engine warmup identity does not match journal")
        if state.get("bar_index") != sum(
            1 for record in retained if record.get("kind") == "bar_received"
        ):
            raise ValueError("live engine bar index does not match journal")

        broker_restore = getattr(self._broker, "restore_checkpoint", None)
        strategy_restore = getattr(self._strategy, "restore_runtime_checkpoint", None)
        if broker_restore is None or strategy_restore is None:
            raise ValueError("runtime components do not support checkpoint restoration")
        broker_restore(checkpoint.get("broker") or {})
        strategy_restore(checkpoint.get("strategy") or {})
        account = PlatformAccountState.from_checkpoint(
            state.get("account") or {},
            ledger=self._broker.ledger,
        )

        self._account = account
        bind_account = getattr(self._strategy, "bind_account", None)
        if bind_account is not None:
            bind_account(self._account.snapshot)
        self._window = rebuilt_window
        self._primary_identity = primary
        self._warmup_identity = warmup
        self._last_input = last_input
        self._bar_index = int(state["bar_index"])
        self._trade_id = int(state["trade_id"])
        self._ledger_cursor = int(state["ledger_cursor"])
        self._cashflow_fill_ids = {
            int(sequence): str(fill_id) for sequence, fill_id in state.get("cashflow_fill_ids", ())
        }
        self._canonical_event_hashes = {
            str(event_id): str(digest) for event_id, digest in state["canonical_event_hashes"]
        }
        if sorted(self._canonical_event_hashes) != state["canonical_event_ids"]:
            raise ValueError("live engine canonical event checkpoint mismatch")
        self._canonical_source_sequences = {
            str(source): int(sequence)
            for source, sequence in state["canonical_source_sequences"].items()
        }
        self._decision_id = state.get("decision_id")
        self._decision_context = deepcopy(state.get("decision_context"))
        self._open_decision_context = deepcopy(state.get("open_decision_context"))
        self._pending_decision_context = deepcopy(state.get("pending_decision_context"))
        pending_setup = state.get("pending_setup")
        open_setup = state.get("open_setup")
        open_entry_fill = state.get("open_entry_fill")
        self._pending_setup = None if pending_setup is None else TradeSetup(**pending_setup)
        self._open_setup = None if open_setup is None else TradeSetup(**open_setup)
        self._open_entry_ts = int(state["open_entry_ts"])
        self._open_entry_fill = None if open_entry_fill is None else Fill(**open_entry_fill)
        self._partial_exit_realized_pnl = float(state["partial_exit_realized_pnl"])
        self._partial_exit_commission = float(state["partial_exit_commission"])
        self._partial_exit_quantity = float(state["partial_exit_quantity"])
        self._partial_exit_notional = float(state["partial_exit_notional"])
        self._partial_exit_spread_cost = float(state["partial_exit_spread_cost"])
        self._partial_exit_slippage_cost = float(state["partial_exit_slippage_cost"])
        self._higher_timeframe_available = bool(state["higher_timeframe_available"])
        self._last_entry_client_order_id = str(state["last_entry_client_order_id"])
        self._entries_halted = bool(state["entries_halted"])
        self._broker_entries_halted = bool(state["broker_entries_halted"])
        self._external_entry_pending = bool(state["external_entry_pending"])
        self._containment_attempted = bool(state["containment_attempted"])
        self._containment_in_progress = bool(state["containment_in_progress"])
        self._containment_confirmed = state.get("containment_confirmed")
        self._terminal_policy = state.get("terminal_policy")
        if bool(state.get("observed_quote_execution")) != self._config.observed_quote_execution:
            raise ValueError("live engine checkpoint paper execution mode mismatch")
        self._last_order_received_at_ms = state.get("last_order_received_at_ms")
        self._last_protection_received_at_ms = state.get("last_protection_received_at_ms")
        if self._engine_checkpoint_state() != state:
            raise ValueError("live engine checkpoint state is not canonical")

    def apply_execution_evidence_update(self, update: ExecutionEvidenceUpdate) -> bool:
        """Journal then apply one idempotent paper-evidence update."""
        if update.funding is not None:
            update.funding.validate_execution_grid(timeframe_ms(self._config.timeframe))
        validate = getattr(self._broker, "validate_execution_evidence_update", None)
        apply = getattr(self._broker, "apply_execution_evidence_update", None)
        if validate is None or apply is None:
            raise RuntimeError("broker cannot accept live execution evidence")
        if not validate(update):
            return False
        self._journal.write(
            "execution_evidence_update",
            update.timestamp_ms,
            update.as_config(),
        )
        if not apply(update):
            raise RuntimeError("broker did not apply journaled execution evidence")
        return True

    def apply_canonical_event_timeline(self, timeline: CanonicalEventTimeline) -> int:
        """Archive one verified market timeline before strategy evaluation.

        Reconnect overlap is idempotent by event identity. Native trade and
        book-delta sequences must continue exactly across acquired batches.
        """
        records, unseen, next_sequences = self._validated_canonical_timeline(timeline)
        for event in unseen:
            self._journal.write(
                "market_event",
                event.timestamp_ms,
                {
                    **records[event.event_id],
                    "timeline_version": timeline.version,
                    "timeline_sha256": timeline.timeline_sha256,
                },
            )
            self._canonical_event_hashes[event.event_id] = _canonical_event_digest(
                records[event.event_id]
            )
        self._canonical_source_sequences = next_sequences
        return len(unseen)

    def validate_canonical_event_timeline(self, timeline: CanonicalEventTimeline) -> int:
        """Validate continuity and retry identity without mutating runtime state."""
        _, unseen, _ = self._validated_canonical_timeline(timeline)
        return len(unseen)

    def _validated_canonical_timeline(
        self, timeline: CanonicalEventTimeline
    ) -> tuple[dict[str, dict], list[CanonicalEvent], dict[str, int]]:
        if not isinstance(timeline, CanonicalEventTimeline):
            raise TypeError("canonical market timeline is required")
        records = {record["event_id"]: record for record in timeline.as_records()}
        unseen = []
        for event in timeline.events:
            digest = _canonical_event_digest(records[event.event_id])
            previous = self._canonical_event_hashes.get(event.event_id)
            if previous is not None:
                if previous != digest:
                    raise ValueError("canonical market event retry changed content")
                continue
            unseen.append(event)
        next_sequences = dict(self._canonical_source_sequences)
        for event in unseen:
            if event.kind not in {"trade", "book_delta"} or event.source_sequence is None:
                continue
            previous = next_sequences.get(event.source)
            if previous is not None and event.source_sequence != previous + 1:
                raise ValueError(
                    f"canonical source sequence gap for {event.source}: "
                    f"expected {previous + 1}, received {event.source_sequence}"
                )
            next_sequences[event.source] = event.source_sequence
        return records, unseen, next_sequences

    def _record_callback(self, kind: str, callback: Callable[[dict], None]):
        def deliver(payload: dict) -> None:
            value = deepcopy(payload)
            raw_timestamp = value.get("timestamp_ms")
            timestamp = int(
                raw_timestamp
                if raw_timestamp is not None
                else (self._window.ts[-1] if self._window.ts else 0)
            )
            if kind == "order_intent":
                value["decision_id"] = self._decision_id
                decision_time = value.get("metadata", {}).get("decision_timestamp_ms")
                if decision_time is not None:
                    timestamp = int(decision_time)
                    value["timestamp_basis"] = "decision_clock"
            if kind == "fill":
                value["fill_id"] = f"{self._session_id}:fill:{self._journal.sequence + 1}"
                for sequence in value.get("metadata", {}).get("cashflow_sequences", []):
                    self._cashflow_fill_ids[sequence] = value["fill_id"]
            self._journal.write(kind, timestamp, value)
            callback(value)

        return deliver

    def _accept_input(self, row: np.ndarray, *, phase: str) -> None:
        ts = int(row[0])
        self._journal.write(
            "bar_received",
            ts,
            {
                "bar_id": f"{self._session_id}:{self._config.timeframe}:{ts}",
                "ohlcv": row.tolist(),
                "phase": phase,
                "available_timestamp_ms": ts + timeframe_ms(self._config.timeframe),
            },
        )
        self._last_input = row.copy()

    def _record_account(self) -> None:
        for entry in self._account.ledger.entries[self._ledger_cursor :]:
            payload = asdict(entry)
            payload["cashflow_id"] = f"{self._session_id}:cashflow:{entry.sequence}"
            payload["fill_id"] = self._cashflow_fill_ids.get(entry.sequence)
            self._journal.write("cashflow", entry.timestamp_ms, payload)
            self._ledger_cursor = entry.sequence
        ts = self._window.ts[-1] if self._window.ts else 0
        if self._config.observed_quote_execution:
            ts = max(ts, self._broker.last_observed_quote_ms or 0)
        self._journal.write("account_snapshot", ts, asdict(self._account.snapshot()))

    def _validate_broker_config(self, config: LiveEngineConfig) -> None:
        if not isinstance(self._broker, PaperBroker):
            return
        self._broker.validate_market_context(exchange=config.exchange, symbol=config.symbol)
        self._broker.validate_execution_grid(timeframe_ms(config.timeframe))
        if self._broker.market != canonical_market(config.market):
            raise ValueError("paper broker market does not match live config")
        if config.execution is not None and self._broker.profile != self._profile:
            raise ValueError("paper broker profile does not match live config")
        self._profile = self._broker.profile
        if config.exchange is not None and self._broker.exchange is not None:
            if self._broker.exchange != config.exchange.strip().lower():
                raise ValueError("paper broker exchange does not match live config")
        requested = execution_evidence_manifest(
            funding=config.funding,
            fee_schedule=config.fee_schedule,
            instrument_specs=config.instrument_specs,
            mark_prices=config.mark_prices,
            execution_proxy=config.execution_proxy,
        )
        for name, evidence in requested.items():
            if (
                evidence["status"] == "supplied"
                and evidence != self._broker.execution_evidence[name]
            ):
                raise ValueError(f"paper broker evidence does not match live config: {name}")

    def _run_identity(self) -> dict:
        return build_run_identity(
            market_identity=self._market_identity,
            primary=self._primary_identity.as_dict(),
            warmup=self._warmup_identity.as_dict(),
            execution_profile=(
                self._profile.as_config()
                if self._broker.target == "paper"
                else getattr(self._broker, "resolved_metadata", {"version": "unavailable"})
            ),
            execution_evidence=getattr(
                self._broker, "execution_evidence", execution_evidence_manifest()
            ),
            strategy_sha256=self._strategy_sha256,
            run_parameters={
                **({"runtime_contract": self._boundaries.as_dict()} if self._boundaries else {}),
                "initial_capital": self._config.initial_capital,
                "max_window": self._config.max_window,
                "higher_timeframe": self._config.higher_timeframe,
                "requires_higher_timeframe": self._config.requires_higher_timeframe,
                "daily_baseline_equity": self._config.daily_baseline_equity,
                "peak_equity": self._config.peak_equity,
                "end_of_data_policy": self._terminal_policy or self._config.end_of_data_policy,
            },
            engine_version=__version__,
            execution_mode=self._broker.target,
        )

    @property
    def profile(self):
        return self._profile

    @property
    def history_tail_ms(self) -> int | None:
        """Opening timestamp of the last preloaded bar, or None when there is none.

        A polling feed resumes from here so the session continues after the
        history it warmed up on instead of replaying it.
        """
        return self._window.ts[-1] if self._window.ts else None

    def run(self, feed: LiveFeed, stop: StopSignal) -> None:
        self._emit(
            EventType.SESSION_START,
            {
                "symbol": self._config.symbol,
                "warmup_range": {
                    "start_ms": self._window.ts[0] if self._window.ts else None,
                    "end_ms": self._window.ts[-1] if self._window.ts else None,
                    "bars": len(self._window.ts),
                },
                "timeframes": {
                    "primary": self._config.timeframe,
                    "higher": self._config.higher_timeframe,
                    "higher_status": (
                        "configured" if self._config.higher_timeframe else "unavailable"
                    ),
                },
            },
        )
        try:
            try:
                for row in feed.bars(stop):
                    if (
                        self._boundaries is not None
                        and float(row[0]) >= self._boundaries.evaluation_end_ms
                    ):
                        break
                    self._process_bar(row)
                if self._boundaries is not None and not stop.is_set():
                    expected = self._boundaries.evaluation_end_ms - timeframe_ms(
                        self._config.timeframe
                    )
                    if not self._window.ts or self._window.ts[-1] != expected:
                        raise ValueError("runtime evaluation coverage is incomplete")
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
                self._finalize(end_of_data=not stop.is_set())
        finally:
            self._emit(EventType.SESSION_END, {})

    def _process_bar(self, row: object) -> None:
        candidate = _validate_ohlcv_row(
            row, previous_timestamp_ms=None, timeframe=self._config.timeframe
        )
        if self._last_input is not None and candidate[0] == self._last_input[0]:
            same = np.array_equal(candidate, self._last_input)
            self._journal.write(
                "bar_duplicate" if same else "bar_conflict",
                int(candidate[0]),
                {"ohlcv": candidate.tolist()},
            )
            if same:
                return
            raise ValueError("conflicting OHLCV reconnect duplicate")
        if self._window.ts and candidate[0] != self._window.ts[-1] + timeframe_ms(
            self._config.timeframe
        ):
            self._journal.write(
                "bar_gap",
                int(candidate[0]),
                {
                    "expected_timestamp_ms": self._window.ts[-1]
                    + timeframe_ms(self._config.timeframe),
                    "received_timestamp_ms": int(candidate[0]),
                },
            )
        row = _validate_ohlcv_row(
            row,
            previous_timestamp_ms=self._window.ts[-1] if self._window.ts else None,
            timeframe=self._config.timeframe,
        )
        ts, o, h, low, c, v = (int(row[0]), *(float(x) for x in row[1:6]))
        if self._boundaries is not None and not self._window.ts:
            if ts != self._boundaries.warmup_start_ms:
                raise ValueError("runtime warmup coverage is incomplete")
        phase = (
            "warmup"
            if self._boundaries and ts < self._boundaries.evaluation_start_ms
            else "evaluation"
        )
        self._accept_input(row, phase=phase)
        self._bar_index += 1
        self._window.append(row)
        if self._boundaries is not None and ts < self._boundaries.evaluation_start_ms:
            self._warmup_identity.append(row)
            self._journal.write("bar_processed", ts, {"phase": phase})
            self._on_checkpoint(self.checkpoint)
            return
        self._primary_identity.append(row)
        process_bar = getattr(self._broker, "process_bar", None)
        paper_fills = (
            process_bar(ts_ms=ts, open=o, high=h, low=low, close=c, volume=v)
            if process_bar is not None
            else []
        )
        for fill in paper_fills:
            self._apply_paper_fill(fill)
        consume_rejection = getattr(self._broker, "consume_entry_rejection", None)
        entry_rejected = False
        rejection = consume_rejection() if consume_rejection is not None else None
        if rejection is not None:
            self._on_paper_entry_rejected(rejection)
            entry_rejected = True
        self._poll_broker_fills()
        account_time = (
            max(
                ts + timeframe_ms(self._config.timeframe),
                self._broker.last_observed_quote_ms or 0,
            )
            if self._config.observed_quote_execution
            else ts
        )
        self._account.on_bar(equity=self._broker_equity(), timestamp_ms=account_time)
        self._inject_state(ts, o, h, low, c, v)
        self._begin_decision(ts)
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
            and not entry_rejected
            and self._broker_position() is None
            and not self._broker_pending()
            and not self._broker_entries_halted
            and (not self._config.requires_higher_timeframe or self._higher_timeframe_available)
        ):
            self._try_enter(ts, c)
        elif self._broker_position() is not None:
            self._update_protection()
        self._emit_bar(ts, o, h, low, c, v)
        self._emit_status()
        self._journal.write("bar_processed", ts, {"phase": phase})
        self._on_checkpoint(self.checkpoint)

    def _update_protection(self) -> None:
        # Snapshot both hooks before touching either leg of a possible OCO pair.
        new_sl = self._strategy.on_sl_update(self._trade_id)
        new_tp = getattr(self._strategy, "on_tp_update", lambda _: None)(self._trade_id)
        if new_sl is None and new_tp is None:
            return
        reason = "dynamic_protection" if new_tp is not None else "dynamic_stop"
        method = getattr(
            self._broker, "modify_protection" if new_tp is not None else "modify_stop", None
        )
        if method is None:
            self._contain_exposure(f"{reason}_unsupported")
            raise RuntimeError("broker cannot apply a dynamic protection update")
        try:
            ack = (
                method(stop_price=new_sl, target_price=new_tp)
                if new_tp is not None
                else method(new_sl)
            )
        except Exception:
            self._contain_exposure(f"{reason}_failed")
            raise
        if ack is not None and ack.status not in {"accepted", "filled"}:
            self._contain_exposure(f"{reason}_rejected")
            raise RuntimeError("broker rejected the dynamic protection update")
        if new_sl is not None:
            position = self._broker_position()
            actual_stop = getattr(position, "stop_price", new_sl)
            if ack is not None:
                actual_stop = float(ack.metadata.get("stop_price", actual_stop))
            self._account.on_stop_update(actual_stop)
        if self._config.observed_quote_execution:
            self._journal.write(
                "paper_protection_update",
                (
                    self._window.ts[-1] + timeframe_ms(self._config.timeframe)
                    if self._window.ts
                    else 0
                ),
                {
                    "stop_price": new_sl,
                    "target_price": new_tp,
                    "ack": None if ack is None else _ack_to_dict(ack),
                },
            )
            self._last_protection_received_at_ms = self._journal.last_received_timestamp_ms
        if self._open_setup is not None:
            if new_tp is not None:
                actual_target = getattr(self._broker_position(), "target_price", new_tp)
                if ack is not None:
                    actual_target = ack.metadata.get("target_price", actual_target)
                self._open_setup.take_profit = float(actual_target)

    def _begin_decision(self, ts: int) -> None:
        self._decision_context = {
            "version": "koval_decision_context_v1",
            "bar_timestamp_ms": ts,
            "decision_timestamp_ms": ts + timeframe_ms(self._config.timeframe),
            "history_start_ms": self._window.ts[0] if self._window.ts else None,
            "history_end_ms": ts + timeframe_ms(self._config.timeframe),
            "history_bars": len(self._window.ts),
            "account": asdict(self._account.snapshot()),
            "indicators": None,
        }
        self._decision_id = self._journal.write(
            "decision", ts + timeframe_ms(self._config.timeframe), self._decision_context
        )
        self._decision_context["decision_id"] = self._decision_id

    def _try_enter(self, ts: int, close: float) -> None:
        if self._decision_context is None or self._decision_context["bar_timestamp_ms"] != ts:
            self._begin_decision(ts)
        direction = None
        if self._strategy.should_long():
            direction = "long"
        elif self._strategy.should_short():
            direction = "short"
        self._entries_halted = self._strategy.was_blocked()
        if direction is None:
            self._journal.write(
                "decision_result",
                ts + timeframe_ms(self._config.timeframe),
                {"decision_id": self._decision_id, "outcome": "no_entry"},
            )
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
            metadata={
                "risk_budget": abs(float(setup.entry_price) - float(setup.stop_loss))
                * float(setup.size),
                "decision_timestamp_ms": ts + timeframe_ms(self._config.timeframe),
            },
        )
        self._journal.write(
            "decision_result",
            ts + timeframe_ms(self._config.timeframe),
            {
                "decision_id": self._decision_id,
                "outcome": "entry",
                "direction": direction,
                "intent_id": intent.intent_id,
                "why_entry": list(setup.why_entry),
            },
        )
        self._on_order_intent(_intent_to_dict(intent))
        if self._config.observed_quote_execution:
            self._last_order_received_at_ms = self._journal.last_received_timestamp_ms
        self._decision_context["why_entry"] = list(setup.why_entry)
        self._pending_decision_context = deepcopy(self._decision_context)
        self._pending_setup = setup
        self._external_entry_pending = True
        self._last_entry_client_order_id = intent.client_order_id
        try:
            ack = self._broker.submit_entry(intent)
        except Exception:
            self._contain_exposure("entry_state_unknown")
            raise
        self._on_order_ack({**_ack_to_dict(ack), "intent_id": intent.intent_id})
        if (
            self._broker.target == "paper"
            and ack.status == "rejected"
            and ack.metadata.get("reason") in {"insufficient_margin", "spot_short_unsupported"}
        ):
            # Deterministic paper refusals leave the strategy free to submit a
            # later affordable/permitted entry. Unknown venue state still halts.
            self._external_entry_pending = False
            self._pending_setup = None
            self._emit(
                EventType.ORDER_REJECTED,
                {
                    "direction": setup.direction,
                    "entry_type": setup.entry_type,
                    "size": setup.size,
                    "price": setup.entry_price,
                    "reason": ack.metadata["reason"],
                },
            )
            return
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

    def observe_paper_quote(
        self,
        *,
        received_at_ms: int,
        venue_event_ms: int,
        bid: float,
        ask: float,
        source_id: str,
        raw_response: dict | None = None,
    ) -> None:
        """Apply one archived Binance book observation after a live paper intent."""
        if not self._config.observed_quote_execution:
            raise ValueError("observed quote execution is not enabled")
        payload = {
            "received_at_ms": received_at_ms,
            "venue_event_ms": venue_event_ms,
            "bid": bid,
            "ask": ask,
            "source_id": source_id,
            "order_received_at_ms": self._last_order_received_at_ms,
            "protection_received_at_ms": self._last_protection_received_at_ms,
        }
        if raw_response is not None:
            payload["raw_response"] = deepcopy(raw_response)
        self.apply_observed_paper_quote(payload)

    def apply_observed_paper_quote(self, payload: dict) -> None:
        """Replay a retained quote using its original receipt clock."""
        if not self._config.observed_quote_execution:
            raise ValueError("observed quote execution is not enabled")
        if not isinstance(payload.get("source_id"), str) or not payload["source_id"]:
            raise ValueError("paper quote requires a retained source id")
        protection_received = payload.get("protection_received_at_ms")
        if (
            self._broker_position() is not None
            and protection_received is not None
            and payload["venue_event_ms"] < protection_received
        ):
            raise ValueError("quote precedes protection placement")
        self._journal.write("paper_quote_observed", payload["received_at_ms"], payload)
        fills = self._broker.process_observed_quote(
            received_at_ms=payload["received_at_ms"],
            venue_event_ms=payload["venue_event_ms"],
            bid=payload["bid"],
            ask=payload["ask"],
            order_received_at_ms=payload["order_received_at_ms"],
        )
        rejection = self._broker.consume_entry_rejection()
        if rejection is not None:
            self._on_paper_entry_rejected(rejection)
        for fill in fills:
            self._apply_paper_fill(fill)
        self._poll_broker_fills()
        self._account.on_bar(equity=self._broker_equity(), timestamp_ms=payload["received_at_ms"])
        if fills:
            self._emit_status()
        self._on_checkpoint(self.checkpoint)

    def watch_orders(self) -> None:
        """Poll the broker between bars so a limit or stop entry is protected
        within seconds of its fill rather than at the next bar close.

        Wire it into a live session by passing it as the polling feed's
        between-bars hook: ``PollingFeed(adapter, ..., between_bars=engine.watch_orders)``.
        A no-op for the paper broker, whose fills are resolved bar-by-bar."""
        if self._broker.target == "paper":
            return
        try:
            self._poll_broker_fills()
        except Exception:
            self._contain_exposure("order_watch_failed")
            raise

    def _poll_broker_fills(self) -> None:
        for fill in self._broker.poll_fills(self._session_id):
            self._on_fill(_broker_fill_to_dict(fill))
            if self._broker.target != "paper":
                self._process_external_fill(fill)

    def _on_paper_entry_rejected(self, detail: str) -> None:
        """Report a stable fill-time rejection reason and keep the session live."""
        setup = self._pending_setup
        self._external_entry_pending = False
        self._pending_setup = None
        reason = str(detail).partition(":")[0].strip() or "paper_entry_rejected"
        self._emit(
            EventType.ORDER_REJECTED,
            {
                "direction": setup.direction if setup is not None else None,
                "entry_type": setup.entry_type if setup is not None else None,
                "size": setup.size if setup is not None else None,
                "price": setup.entry_price if setup is not None else None,
                "reason": reason,
            },
        )

    def _on_open(self, fill: Fill) -> None:
        self._external_entry_pending = False
        self._trade_id += 1
        setup = self._pending_setup
        self._open_setup = setup
        self._open_decision_context = deepcopy(self._pending_decision_context)
        self._open_entry_ts = fill.timestamp_ms
        self._open_entry_fill = fill
        self._pending_setup = None
        if setup is not None:
            try:
                _validate_fill_against_protection(
                    setup, fill, paper_matched=isinstance(self._broker, PaperBroker)
                )
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
            actual_setup = copy(setup)
            actual_setup.entry_price = fill.price
            actual_setup.size = fill.quantity
            if isinstance(self._broker, PaperBroker):
                reference = self._broker.protection_reference
                if reference is not None:
                    actual_setup.stop_loss = reference.stop_price
                    actual_setup.take_profit = reference.target_price
            self._account.on_open(
                side=fill.side,
                entry_price=fill.price,
                quantity=fill.quantity,
                current_stop=actual_setup.stop_loss,
                margin=fill.margin or float(getattr(self._broker, "margin_used", 0.0)),
            )
            if fill.commission and not self._uses_broker_ledger:
                self._account.on_fee(
                    fill.commission,
                    timestamp_ms=fill.timestamp_ms,
                    reference_id=self._last_entry_client_order_id,
                )
            self._strategy.on_open_position(self._trade_id, actual_setup)
            self._place_protection(actual_setup, fill)
        self._emit(
            EventType.TRADE_OPENED,
            {
                "direction": "long" if fill.side == "buy" else "short",
                "entry_price": fill.price,
                "size": fill.quantity,
                "why_entry": list(setup.why_entry) if setup else [],
            },
            timestamp_ms=fill.timestamp_ms if self._config.observed_quote_execution else None,
        )

    def _apply_paper_fill(self, fill: Fill) -> None:
        """Apply one paper fill delta without treating a partial as terminal."""
        if fill.kind == "entry":
            if self._account.snapshot().open_position is None:
                self._on_open(fill)
            else:
                self._on_entry_fill_delta(fill)
            return
        if fill.status == "partial":
            self._on_partial_close(fill)
        else:
            self._on_close(fill)

    def _on_entry_fill_delta(self, fill: Fill) -> None:
        setup = self._open_setup
        current = self._open_entry_fill
        if setup is None or current is None:
            raise RuntimeError("additional entry fill has no open setup to resize")
        _validate_fill_against_protection(
            setup, fill, paper_matched=isinstance(self._broker, PaperBroker)
        )
        total_quantity = current.quantity + fill.quantity
        average_price = (
            current.price * current.quantity + fill.price * fill.quantity
        ) / total_quantity
        aggregate = replace(
            current,
            price=average_price,
            quantity=total_quantity,
            realized_pnl=current.realized_pnl + fill.realized_pnl,
            commission=current.commission + fill.commission,
            spread_cost=current.spread_cost + fill.spread_cost,
            slippage_cost=current.slippage_cost + fill.slippage_cost,
            margin=current.margin + fill.margin,
            status=fill.status,
            cumulative_quantity=fill.cumulative_quantity,
        )
        self._open_entry_fill = aggregate
        self._account.on_entry_fill(
            entry_price=fill.price,
            quantity=fill.quantity,
            margin=fill.margin,
        )
        if fill.commission and not self._uses_broker_ledger:
            self._account.on_fee(
                fill.commission,
                timestamp_ms=fill.timestamp_ms,
                reference_id=self._last_entry_client_order_id,
            )
        broker_position = getattr(self._broker, "position", None)
        self._place_protection(
            setup,
            aggregate,
            stop_price=getattr(broker_position, "stop_price", setup.stop_loss),
            target_price=getattr(broker_position, "target_price", setup.take_profit),
        )

    def _on_partial_close(self, fill: Fill) -> None:
        gross = fill.realized_pnl + fill.commission
        self._account.on_partial_close(
            quantity=fill.quantity,
            realized_pnl=gross,
            timestamp_ms=fill.timestamp_ms,
            reference_id=self._last_entry_client_order_id,
            record_ledger=not self._uses_broker_ledger,
        )
        if fill.commission and not self._uses_broker_ledger:
            self._account.on_fee(
                fill.commission,
                timestamp_ms=fill.timestamp_ms,
                reference_id=self._last_entry_client_order_id,
            )
        self._partial_exit_realized_pnl += fill.realized_pnl
        self._partial_exit_commission += fill.commission
        self._partial_exit_quantity += fill.quantity
        self._partial_exit_notional += fill.price * fill.quantity
        self._partial_exit_spread_cost += fill.spread_cost
        self._partial_exit_slippage_cost += fill.slippage_cost

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
            commission=_venue_commission(fill),
            reference_price=price,
        )

    def _place_protection(
        self,
        setup: TradeSetup,
        fill: Fill,
        *,
        stop_price: float | None = None,
        target_price: float | None = None,
    ) -> None:
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
            stop_price=str(setup.stop_loss if stop_price is None else stop_price),
            target_price=str(setup.take_profit if target_price is None else target_price),
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
        if self._config.observed_quote_execution:
            self._last_protection_received_at_ms = self._journal.last_received_timestamp_ms

    def _on_close(self, fill: Fill) -> None:
        record = self._trade_record(self._open_setup, fill)
        self._account.on_close(
            realized_pnl=fill.realized_pnl + fill.commission + fill.liquidation_fee,
            timestamp_ms=fill.timestamp_ms,
            reference_id=self._last_entry_client_order_id,
            record_ledger=not self._uses_broker_ledger,
        )
        if fill.commission and not self._uses_broker_ledger:
            self._account.on_fee(
                fill.commission,
                timestamp_ms=fill.timestamp_ms,
                reference_id=self._last_entry_client_order_id,
            )
        if fill.liquidation_fee and not self._uses_broker_ledger:
            self._account.on_liquidation_fee(
                fill.liquidation_fee,
                timestamp_ms=fill.timestamp_ms,
                reference_id=self._last_entry_client_order_id,
            )
        self._strategy.on_close_position(self._trade_id, {"pnl": record["pnl"]})
        self._open_setup = None
        self._open_entry_fill = None
        self._partial_exit_realized_pnl = 0.0
        self._partial_exit_commission = 0.0
        self._partial_exit_quantity = 0.0
        self._partial_exit_notional = 0.0
        self._partial_exit_spread_cost = 0.0
        self._partial_exit_slippage_cost = 0.0
        self._emit(
            EventType.TRADE_CLOSED,
            {
                "pnl": record["pnl"],
                "exit_price": fill.price,
                "exit_reason": record["reason"],
                "entry_price": record["entry_price"],
                "size": record["size"],
            },
            timestamp_ms=fill.timestamp_ms if self._config.observed_quote_execution else None,
        )
        self._on_trade(record)

    def _trade_record(self, setup: TradeSetup | None, fill: Fill) -> dict:
        direction = "long" if fill.side == "buy" else "short"
        reason = {
            "stop_loss": "sl",
            "take_profit": "tp",
            "manual_close": "manual",
            "liquidation": "liquidation",
        }.get(fill.kind, "other")
        # The record reports what actually filled, never the setup's request.
        entry = self._open_entry_fill
        entry_price = (
            entry.price if entry is not None else (setup.entry_price if setup else fill.price)
        )
        entry_commission = entry.commission if entry is not None else 0.0
        exit_quantity = self._partial_exit_quantity + fill.quantity
        exit_price = (
            (self._partial_exit_notional + fill.price * fill.quantity) / exit_quantity
            if exit_quantity
            else fill.price
        )
        exit_commission = self._partial_exit_commission + fill.commission
        exit_realized = self._partial_exit_realized_pnl + fill.realized_pnl
        sign = 1.0 if fill.side == "buy" else -1.0
        gross = (exit_price - entry_price) * exit_quantity * sign
        net = exit_realized - entry_commission
        return {
            "trade_id": f"{self._session_id}:trade:{self._trade_id}",
            "entry_order_id": self._last_entry_client_order_id,
            "decision_context": deepcopy(self._open_decision_context),
            "entry_time": _iso(self._open_entry_ts),
            "exit_time": _iso(fill.timestamp_ms),
            "direction": direction,
            "entry_price": entry_price,
            "entry_reference_price": entry.reference_price if entry is not None else None,
            "exit_price": exit_price,
            "exit_reference_price": fill.reference_price,
            "size": exit_quantity,
            "pnl": net,
            "gross_price_pnl": gross,
            "commission": entry_commission + exit_commission,
            "liquidation_fee": fill.liquidation_fee,
            "execution_costs": {
                "spread_cost": (entry.spread_cost if entry is not None else 0.0)
                + fill.spread_cost
                + self._partial_exit_spread_cost,
                "slippage_cost": (entry.slippage_cost if entry is not None else 0.0)
                + fill.slippage_cost
                + self._partial_exit_slippage_cost,
                "commission": entry_commission + exit_commission,
                "liquidation_fee": fill.liquidation_fee,
            },
            "pnl_pct": (net / (entry_price * exit_quantity) * 100.0)
            if entry_price and exit_quantity
            else 0.0,
            "reason": reason,
            "exit_reason_text": {
                "stop_loss": "Stop Loss",
                "take_profit": "Take Profit",
                "manual_close": "Session stop",
                "liquidation": "Liquidation",
            }.get(fill.kind, "Other"),
            "execution_profile": getattr(
                self._broker, "resolved_metadata", self._profile.as_config()
            ),
            "run_identity": self._run_identity(),
        }

    def _finalize(self, *, require_confirmation: bool = True, end_of_data: bool = False) -> None:
        position = self._broker_position()
        retain_observed = self._config.observed_quote_execution and position is not None
        retain = retain_observed or (
            end_of_data and self._config.end_of_data_policy == "mark_at_last_close"
        )
        self._terminal_policy = (
            "retain_observed_paper_position"
            if retain_observed
            else "mark_at_last_close"
            if retain
            else "flatten_at_last_close"
        )
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
        if position is not None and not retain:
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
        s.decision_timestamp_ms = ts + timeframe_ms(self._config.timeframe)
        s.closes = np.array(self._window.c, dtype=float)
        s.highs = np.array(self._window.h, dtype=float)
        s.lows = np.array(self._window.low, dtype=float)
        s.opens = np.array(self._window.o, dtype=float)
        s.volumes = np.array(self._window.v, dtype=float)
        if self._config.higher_timeframe is None:
            self._higher_timeframe_available = False
            s.htf_closes = s.htf_highs = s.htf_lows = s.htf_opens = s.htf_volumes = None
        else:
            source = np.column_stack(
                (
                    self._window.ts,
                    self._window.o,
                    self._window.h,
                    self._window.low,
                    self._window.c,
                    self._window.v,
                )
            )
            higher = confirmed_higher_timeframe_bars(
                source,
                source_timeframe=self._config.timeframe,
                target_timeframe=self._config.higher_timeframe,
                decision_time_ms=ts + timeframe_ms(self._config.timeframe),
            )
            if higher.size == 0:
                self._higher_timeframe_available = False
                s.htf_closes = s.htf_highs = s.htf_lows = s.htf_opens = s.htf_volumes = None
            else:
                self._higher_timeframe_available = True
                s.htf_opens = higher[:, 1].copy()
                s.htf_highs = higher[:, 2].copy()
                s.htf_lows = higher[:, 3].copy()
                s.htf_closes = higher[:, 4].copy()
                s.htf_volumes = higher[:, 5].copy()
        s.account_value = self._broker_equity()
        pos = self._broker_position()
        s.position_size = float(pos.quantity) if pos else 0.0
        s.position_direction = None if pos is None else ("long" if pos.side == "buy" else "short")

    def _emit(
        self, event_type: EventType, payload: dict, *, timestamp_ms: int | None = None
    ) -> None:
        ts = (
            timestamp_ms
            if timestamp_ms is not None
            else self._window.ts[-1]
            if self._window.ts
            else 0
        )
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

    def _emit_bar(self, ts, o, h, low, c, v) -> None:
        self._on_bar(
            {
                "ts": ts,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": v,
                "equity": self._broker_equity(),
                "unrealized_pnl": self._broker_equity() - self._broker_balance(),
            }
        )

    def _emit_status(self, *, terminal: bool = False) -> None:
        self._record_account()
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
                    "daily_loss_pct": snap.daily_loss_pct,
                    "drawdown_pct": snap.drawdown_pct,
                    "fees": snap.fees,
                    "funding": snap.funding,
                    "trade_realized_pnl": snap.trade_realized_pnl,
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
                "run_identity": self._run_identity(),
                "execution_profile": getattr(
                    self._broker, "resolved_metadata", self._profile.as_config()
                ),
                "margin_used": snap.margin_used,
                "free_margin": snap.free_margin,
                "higher_timeframe": {
                    "configured": self._config.higher_timeframe,
                    "required": self._config.requires_higher_timeframe,
                    "available": self._higher_timeframe_available,
                },
                "containment_attempted": self._containment_attempted,
                "containment_confirmed": self._containment_confirmed,
                "terminal_policy": self._terminal_policy if terminal else None,
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
