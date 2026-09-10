import numpy as np
import pytest
import requests

from koval.engine.broker import BrokerFill, BrokerOrderAck
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.engine.funding import FundingRecord, build_funding_series
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.engine.paper_broker import Fill, PaperBroker
from tests.engine.live_fixtures import ema_cross_graph


class _RawRowFeed:
    """Yields rows verbatim. ``LiveFeed`` is a Protocol, so the engine must
    validate every row itself rather than trusting a well-behaved feed."""

    def __init__(self, rows):
        self._rows = rows

    def bars(self, stop):
        for row in self._rows:
            if stop.is_set():
                return
            yield row


class _FakeSetup:
    direction = "long"
    entry_price = 100.0
    stop_loss = 95.0
    take_profit = 110.0
    size = 1.0
    entry_type = "market"
    why_entry = ["test"]


class _AlwaysLongStrategy:
    config = {}

    def should_long(self):
        return True

    def should_short(self):
        return False

    def was_blocked(self):
        return False

    def _execute_filters(self):
        return True

    def go_long(self):
        return _FakeSetup()

    def go_short(self):
        raise AssertionError("unexpected short")

    def on_sl_update(self, trade_id):
        return None

    def on_open_position(self, trade_id, setup):
        return None

    def on_close_position(self, trade_id, result):
        return None


class _ConfiguredSetupStrategy(_AlwaysLongStrategy):
    def __init__(self, **overrides):
        self._setup = _FakeSetup()
        for name, value in overrides.items():
            setattr(self._setup, name, value)

    def go_long(self):
        return self._setup


class _SingleEntryStrategy(_ConfiguredSetupStrategy):
    def __init__(self, **overrides):
        super().__init__(**overrides)
        self.open_count = 0

    def should_long(self):
        return self.open_count == 0

    def on_open_position(self, trade_id, setup):
        self.open_count += 1


class _NoTradeHtfStrategy(_AlwaysLongStrategy):
    def __init__(self):
        self.seen_htf = []

    def should_long(self):
        return False

    def on_bar(self):
        self.seen_htf.append(None if self.htf_closes is None else self.htf_closes.copy())


class _RecordingBroker:
    target = "binance_sandbox"

    def __init__(self):
        self.submitted = []
        self.position = None
        self.pending = False
        self.equity = 10_000.0
        self.balance = 10_000.0

    def submit_entry(self, intent):
        self.submitted.append(intent)
        return BrokerOrderAck(
            session_id=intent.session_id,
            client_order_id=intent.client_order_id,
            exchange_order_id="1",
            status="accepted",
            target=self.target,
        )

    def process_bar(self, **kwargs):
        return []

    def fill_market_if_pending(self, **kwargs):
        return None

    def poll_fills(self, session_id):
        return []

    def place_protection(self, intent):
        return []

    def reconcile(self, session_id, intents):
        raise AssertionError("not used")

    def cancel_all(self, symbol, session_id):
        return []

    def flatten(self, symbol, session_id):
        return None


class _ImmediateFillBroker(_RecordingBroker):
    def __init__(self):
        super().__init__()
        self._fills = []
        self.protection_intents = []

    def submit_entry(self, intent):
        self.submitted.append(intent)
        self._fills.append(
            BrokerFill(
                session_id=intent.session_id,
                client_order_id=intent.client_order_id,
                exchange_order_id="1",
                symbol=intent.symbol,
                side=intent.side,
                status="filled",
                role="entry",
                quantity=intent.quantity,
                price=intent.price,
                timestamp_ms=0,
            )
        )
        return BrokerOrderAck(
            session_id=intent.session_id,
            client_order_id=intent.client_order_id,
            exchange_order_id="1",
            status="filled",
            target=self.target,
        )

    def poll_fills(self, session_id):
        fills, self._fills = self._fills, []
        return fills

    def place_protection(self, intent):
        self.protection_intents.append(intent)
        return [
            BrokerOrderAck(
                session_id=intent.session_id,
                client_order_id=intent.stop_client_order_id,
                exchange_order_id="2",
                status="accepted",
                target=self.target,
            ),
            BrokerOrderAck(
                session_id=intent.session_id,
                client_order_id=intent.target_client_order_id,
                exchange_order_id="3",
                status="accepted",
                target=self.target,
            ),
        ]


def _ramp_then_drop(n_up: int, n_down: int) -> np.ndarray:
    """A clean up-ramp then down-ramp - guarantees an EMA crossover both ways."""
    rows, price, ts = [], 100.0, 0
    for _ in range(n_up):
        price += 1.0
        rows.append([ts, price - 0.5, price + 0.5, price - 0.6, price, 10.0])
        ts += 60_000
    for _ in range(n_down):
        price -= 1.0
        rows.append([ts, price + 0.5, price + 0.6, price - 0.5, price, 10.0])
        ts += 60_000
    return np.array(rows, dtype=float)


def _run(graph, candles):
    events, trades, bars = [], [], []
    eng = LiveEngine(
        graph,
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        on_event=events.append,
        on_trade=trades.append,
        on_bar=bars.append,
    )
    eng.run(ReplayFeed(candles, delay_seconds=0.0), StopSignal())
    return events, trades, bars


def test_run_streams_one_bar_update_per_bar():
    candles = _ramp_then_drop(20, 20)
    _events, _trades, bars = _run(ema_cross_graph(), candles)
    assert len(bars) == len(candles)
    assert all("equity" in b and "close" in b for b in bars)


def test_live_engine_passes_execution_proxy_to_its_default_paper_broker(monkeypatch):
    from decimal import Decimal

    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    proxy = ExecutionProxyConfig(
        maximum_volume_participation=Decimal("0.1"),
        entry_remainder_policy="carry",
        latency=ExecutionLatency(),
    )
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1m",
            initial_capital=10_000.0,
            execution={
                "version": "paper_ohlcv_realistic_v2",
                "commission_bps": 4.0,
                "spread_bps": 0.0,
                "slippage_bps": 1.0,
            },
            execution_proxy=proxy,
        ),
    )
    assert engine._broker.resolved_metadata["execution_model"] == "fixed_ohlcv_proxy"  # noqa: SLF001


def test_live_account_uses_the_paper_brokers_authoritative_funding_ledger(monkeypatch):
    from decimal import Decimal

    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _SingleEntryStrategy()
    )
    records = tuple(
        FundingRecord(
            symbol="BTCUSDT",
            rate=Decimal("0.0001"),
            settlement_timestamp_ms=timestamp,
            settlement_mark_price=Decimal("100"),
            interval_ms=60_000,
            source="test",
        )
        for timestamp in (0, 60_000, 120_000)
    )
    funding = build_funding_series(
        records,
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=0,
        requested_end_ms=120_000,
    )
    statuses = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1m",
            initial_capital=10_000.0,
            execution={
                "version": "paper_ohlcv_realistic_v2",
                "commission_bps": 0.0,
                "spread_bps": 0.0,
                "slippage_bps": 0.0,
            },
            funding=funding,
        ),
        on_status=statuses.append,
    )

    engine.run(
        ReplayFeed(
            np.array(
                [
                    [0, 100, 101, 99, 100, 10],
                    [60_000, 100, 101, 99, 100, 10],
                    [120_000, 100, 101, 99, 100, 10],
                ],
                dtype=float,
            )
        ),
        StopSignal(),
    )

    assert statuses[-1]["metrics"]["funding"] == pytest.approx(-0.01)
    assert statuses[-1]["metrics"]["balance"] == pytest.approx(9_999.99)


def test_live_engine_injects_only_confirmed_higher_timeframe_bars(monkeypatch):
    strategy = _NoTradeHtfStrategy()
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    events = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1m",
            higher_timeframe="5m",
            initial_capital=10_000.0,
        ),
        on_event=events.append,
    )

    engine.run(ReplayFeed(_ramp_then_drop(7, 0)), StopSignal())

    assert all(value is None for value in strategy.seen_htf[:4])
    assert strategy.seen_htf[4].tolist() == [105.0]
    assert strategy.seen_htf[-1].tolist() == [105.0]
    assert events[0]["payload"]["timeframes"] == {
        "primary": "1m",
        "higher": "5m",
        "higher_status": "configured",
    }


def test_higher_timeframe_survives_a_feed_that_does_not_start_on_a_bucket_boundary(
    monkeypatch,
):
    """A venue returns whatever bars its history had, and the rolling window
    slides, so the window rarely begins on a higher-timeframe boundary. That
    must not abort the session."""
    strategy = _NoTradeHtfStrategy()
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    candles = _ramp_then_drop(10, 0)
    candles[:, 0] += 2 * 60_000
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1m",
            higher_timeframe="5m",
            initial_capital=10_000.0,
        ),
    )

    engine.run(ReplayFeed(candles), StopSignal())

    assert all(value is None for value in strategy.seen_htf[:7])
    assert strategy.seen_htf[-1].tolist() == [108.0]


def test_required_higher_timeframe_blocks_entries_until_confirmed_data_exists(
    monkeypatch,
):
    strategy = _SingleEntryStrategy()
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    events, statuses = [], []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1m",
            higher_timeframe="5m",
            requires_higher_timeframe=True,
            initial_capital=10_000.0,
        ),
        on_event=events.append,
        on_status=statuses.append,
    )

    engine.run(ReplayFeed(_ramp_then_drop(4, 0)), StopSignal())

    assert "TRADE_OPENED" not in [event["event_type"] for event in events]
    assert statuses[-1]["higher_timeframe"] == {
        "configured": "5m",
        "required": True,
        "available": False,
    }


def test_run_emits_signal_and_opens_then_closes_a_trade():
    candles = _ramp_then_drop(20, 20)
    events, trades, _bars = _run(ema_cross_graph(), candles)
    kinds = [e["event_type"] for e in events]
    assert "SIGNAL_DETECTED" in kinds
    assert "TRADE_OPENED" in kinds
    assert trades, "expected at least one closed trade"
    assert {"entry_price", "exit_price", "pnl", "direction"} <= set(trades[0])


def test_trade_closed_event_carries_reason_entry_and_size():
    candles = _ramp_then_drop(20, 20)
    events, trades, _bars = _run(ema_cross_graph(), candles)
    closed = [e for e in events if e["event_type"] == "TRADE_CLOSED"]
    assert closed, "expected a TRADE_CLOSED event"
    payload = closed[0]["payload"]
    assert {"pnl", "exit_price", "exit_reason", "entry_price", "size"} <= set(payload)
    assert payload["exit_reason"] in {"tp", "sl", "manual", "other"}
    # The streamed close event and the persisted trade record share one source.
    assert payload["entry_price"] == trades[0]["entry_price"]
    assert payload["size"] == trades[0]["size"]
    assert payload["exit_reason"] == trades[0]["reason"]


def test_stop_before_first_bar_runs_nothing():
    stop = StopSignal()
    stop.set()
    events = []
    eng = LiveEngine(
        ema_cross_graph(),
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        on_event=events.append,
    )
    eng.run(ReplayFeed(_ramp_then_drop(10, 10), delay_seconds=0.0), stop)
    assert events == [] or all(e["event_type"] != "SIGNAL_DETECTED" for e in events)


@pytest.mark.parametrize(
    "row",
    [
        [0, 100, 99, 101, 100, 1],
        [0, 100, 101, 99, float("nan"), 1],
        [0, 100, 101, 99, 100, -1],
        [0, 100, 101, 99, 100],
    ],
)
def test_run_rejects_malformed_ohlcv_and_still_ends_session(row):
    events = []
    engine = LiveEngine(
        ema_cross_graph(),
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        on_event=events.append,
    )

    with pytest.raises(ValueError, match="OHLCV"):
        engine.run(_RawRowFeed([row]), StopSignal())

    assert events[-1]["event_type"] == "SESSION_END"


def test_run_rejects_non_contiguous_bar_timestamp():
    candles = np.asarray(
        [
            [0, 100, 101, 99, 100, 1],
            [120_000, 100, 101, 99, 100, 1],
        ],
        dtype=float,
    )
    engine = LiveEngine(
        ema_cross_graph(),
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
    )

    with pytest.raises(ValueError, match="continuity"):
        engine.run(ReplayFeed(candles), StopSignal())


def test_order_intent_callback_runs_before_broker_submit(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    broker = _RecordingBroker()
    calls = []
    intents = []
    eng = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_order_intent=lambda intent: (calls.append("intent"), intents.append(intent)),
        on_order_ack=lambda ack: calls.append("ack"),
    )

    eng._try_enter(0, 100.0)

    assert calls == ["intent", "ack"]
    assert len(broker.submitted) == 1
    assert intents[0]["client_order_id"] == broker.submitted[0].client_order_id
    assert broker.submitted[0].client_order_id.startswith("kv-")


def test_order_intent_persistence_failure_prevents_broker_submit(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    broker = _RecordingBroker()

    def fail(_intent):
        raise RuntimeError("journal unavailable")

    eng = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_order_intent=fail,
    )

    with pytest.raises(RuntimeError, match="journal unavailable"):
        eng._try_enter(0, 100.0)

    assert broker.submitted == []


def test_immediate_external_market_fill_is_protected_before_try_enter_returns(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    broker = _ImmediateFillBroker()
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
    )

    engine._try_enter(0, 100.0)

    assert len(broker.protection_intents) == 1
    assert engine._account.snapshot().open_position is not None  # noqa: SLF001


def test_market_fill_outside_configured_bracket_is_contained_before_protection(
    monkeypatch,
):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class SlippedPastTargetBroker(_ImmediateFillBroker):
        def submit_entry(self, intent):
            ack = super().submit_entry(intent)
            original = self._fills[0]
            self._fills[0] = BrokerFill(
                session_id=original.session_id,
                client_order_id=original.client_order_id,
                exchange_order_id=original.exchange_order_id,
                symbol=original.symbol,
                side=original.side,
                status=original.status,
                role=original.role,
                quantity=original.quantity,
                price="120",
                timestamp_ms=original.timestamp_ms,
            )
            return ack

    broker = SlippedPastTargetBroker()
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
    )

    with pytest.raises(ValueError, match="outside configured protection"):
        engine._try_enter(0, 100.0)

    assert broker.protection_intents == []
    assert engine._containment_attempted is True  # noqa: SLF001


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_window": 0}, "max_window"),
        ({"max_window": -1}, "max_window"),
        ({"initial_capital": 0.0}, "initial_capital"),
        ({"initial_capital": float("inf")}, "initial_capital"),
        ({"symbol": ""}, "symbol"),
    ],
)
def test_invalid_live_engine_config_is_rejected_before_strategy_assembly(
    monkeypatch, overrides, message
):
    assembled = False

    def assemble(_graph):
        nonlocal assembled
        assembled = True
        return _AlwaysLongStrategy()

    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", assemble)
    values = {
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "initial_capital": 10_000.0,
        **overrides,
    }

    with pytest.raises(ValueError, match=message):
        LiveEngine({"blocks": [], "connections": []}, LiveEngineConfig(**values))

    assert assembled is False


def test_live_engine_rejects_rows_not_aligned_to_timeframe_boundary(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
    )

    with pytest.raises(ValueError, match="timeframe boundary"):
        engine._process_bar(np.array([30_000, 100, 101, 99, 100, 1], dtype=float))


def test_target_only_ack_is_not_accepted_as_complete_protection(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class TargetOnlyBroker(_ImmediateFillBroker):
        def __init__(self):
            super().__init__()
            self.flatten_calls = []

        def place_protection(self, intent):
            return [
                BrokerOrderAck(
                    session_id=intent.session_id,
                    client_order_id=intent.target_client_order_id,
                    exchange_order_id="3",
                    status="accepted",
                    target=self.target,
                )
            ]

        def flatten(self, symbol, session_id):
            self.flatten_calls.append((symbol, session_id))
            return None

    broker = TargetOnlyBroker()
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
    )

    with pytest.raises(RuntimeError, match="protection"):
        engine._try_enter(0, 100.0)

    assert broker.flatten_calls == [("BTCUSDT", "session-1")]


@pytest.mark.parametrize(
    "overrides",
    [
        {"direction": "short"},
        {"entry_price": float("nan")},
        {"entry_price": 0.0},
        {"stop_loss": 105.0},
        {"take_profit": None},
        {"take_profit": 95.0},
        {"size": None},
        {"size": 0.0},
        {"entry_type": "iceberg"},
    ],
)
def test_invalid_live_trade_setup_is_rejected_before_order_persistence(monkeypatch, overrides):
    strategy = _ConfiguredSetupStrategy(**overrides)
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    broker = _RecordingBroker()
    intents = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        broker=broker,
        on_order_intent=intents.append,
    )

    with pytest.raises(ValueError, match="live trade setup"):
        engine._try_enter(0, 100.0)

    assert intents == []
    assert broker.submitted == []


def test_external_pending_entry_is_canceled_when_session_finishes(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class PendingBroker(_RecordingBroker):
        def __init__(self):
            super().__init__()
            self.cancel_calls = []

        def submit_entry(self, intent):
            ack = super().submit_entry(intent)
            self.pending = True
            return ack

        def cancel_all(self, symbol, session_id):
            self.cancel_calls.append((symbol, session_id))
            self.pending = False
            return []

    broker = PendingBroker()
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
    )

    engine.run(
        ReplayFeed(np.asarray([[0, 100, 101, 99, 100, 1]], dtype=float)),
        StopSignal(),
    )

    assert broker.cancel_calls == [("BTCUSDT", "session-1")]
    assert broker.pending is False


def test_paper_pending_entry_is_canceled_when_session_finishes(monkeypatch):
    strategy = _ConfiguredSetupStrategy(
        entry_type="limit",
        entry_price=90.0,
        stop_loss=80.0,
        take_profit=110.0,
    )
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    broker = PaperBroker(10_000.0)
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
    )

    engine.run(
        ReplayFeed(np.asarray([[0, 100, 101, 99, 100, 1]], dtype=float)),
        StopSignal(),
    )

    assert broker.pending is False


def test_strategy_can_cancel_a_working_entry_without_immediate_resubmission(monkeypatch):
    strategy = _ConfiguredSetupStrategy(
        entry_type="limit",
        entry_price=90.0,
        stop_loss=80.0,
        take_profit=110.0,
    )
    strategy.should_cancel_entry = lambda: True
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    broker = PaperBroker(10_000.0)
    intents = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_order_intent=intents.append,
    )

    engine._process_bar(np.asarray([0, 100, 101, 99, 100, 1], dtype=float))
    engine._process_bar(np.asarray([60_000, 100, 101, 99, 100, 1], dtype=float))

    assert broker.pending is False
    assert len(intents) == 1


def test_external_broker_does_not_resubmit_entry_before_fill(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    broker = _RecordingBroker()
    delattr(broker, "pending")
    eng = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_order_intent=lambda intent: None,
    )

    eng._process_bar(np.array([0, 100, 101, 99, 100, 1], dtype=float))
    eng._process_bar(np.array([60_000, 100, 101, 99, 100, 1], dtype=float))

    assert len(broker.submitted) == 1


def test_paper_broker_fill_is_not_replayed_as_external_entry(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    events = []
    eng = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        on_event=events.append,
    )

    eng._process_bar(np.array([0, 100, 101, 99, 100, 1], dtype=float))
    eng._process_bar(np.array([60_000, 100, 101, 99, 100, 1], dtype=float))

    opened = [event for event in events if event["event_type"] == "TRADE_OPENED"]
    assert len(opened) == 1


@pytest.mark.parametrize(
    ("exit_row", "exit_role", "exit_price", "identity_key"),
    [
        ([60_000, 100, 101, 94, 96, 1], "stop", "95.0", "stop_client_order_id"),
        ([60_000, 100, 111, 99, 109, 1], "target", "110.0", "target_client_order_id"),
    ],
)
def test_paper_protective_fills_reach_on_fill_once_with_persisted_identity(
    monkeypatch, exit_row, exit_role, exit_price, identity_key
):
    strategy = _SingleEntryStrategy()
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    intents = []
    audits = []
    fills = []
    events = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        on_order_intent=intents.append,
        on_audit=audits.append,
        on_fill=fills.append,
        on_event=events.append,
    )

    engine._process_bar(np.array([0, 100, 101, 99, 100, 1], dtype=float))
    engine._process_bar(np.array(exit_row, dtype=float))

    protection = next(audit for audit in audits if audit["type"] == "protection_intent")
    assert [(fill["role"], fill["status"]) for fill in fills] == [
        ("entry", "filled"),
        (exit_role, "filled"),
    ]
    assert fills[0]["client_order_id"] == intents[0]["client_order_id"]
    assert fills[1]["client_order_id"] == protection["payload"][identity_key]
    assert fills[1]["session_id"] == "session-1"
    assert fills[1]["symbol"] == "BTCUSDT"
    assert fills[1]["quantity"] == "1.0"
    assert fills[1]["price"] == exit_price
    assert len([event for event in events if event["event_type"] == "TRADE_OPENED"]) == 1


def test_same_bar_paper_exit_uses_protective_client_identity(monkeypatch):
    strategy = _SingleEntryStrategy(entry_type="limit")
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    audits = []
    fills = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        on_audit=audits.append,
        on_fill=fills.append,
    )

    engine._process_bar(np.array([0, 100, 101, 99, 100, 1], dtype=float))
    engine._process_bar(np.array([60_000, 100, 101, 94, 96, 1], dtype=float))

    protection = next(audit for audit in audits if audit["type"] == "protection_intent")
    assert [fill["role"] for fill in fills] == ["entry", "stop"]
    assert fills[1]["client_order_id"] == protection["payload"]["stop_client_order_id"]
    assert fills[1]["price"] == "95.0"


def test_final_paper_flatten_reaches_on_fill_once_with_session_identity(monkeypatch):
    strategy = _SingleEntryStrategy()
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    intents = []
    fills = []
    events = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        on_order_intent=intents.append,
        on_fill=fills.append,
        on_event=events.append,
    )

    engine.run(
        ReplayFeed(np.array([[0, 100, 101, 99, 100, 1]], dtype=float)),
        StopSignal(),
    )

    assert [(fill["role"], fill["status"]) for fill in fills] == [
        ("entry", "filled"),
        ("flatten", "filled"),
    ]
    assert fills[0]["client_order_id"] == intents[0]["client_order_id"]
    assert fills[1]["client_order_id"] == "paper-flatten-session-1"
    assert fills[1]["session_id"] == "session-1"
    assert fills[1]["symbol"] == "BTCUSDT"
    assert fills[1]["quantity"] == "1.0"
    assert fills[1]["price"] == "100.0"
    assert len([event for event in events if event["event_type"] == "TRADE_OPENED"]) == 1


def test_rejected_entry_ack_halts_entries_and_emits_incident(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class RejectedBroker(_RecordingBroker):
        def submit_entry(self, intent):
            self.submitted.append(intent)
            return BrokerOrderAck(
                session_id=intent.session_id,
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                status="rejected",
                target=self.target,
            )

    broker = RejectedBroker()
    incidents = []
    statuses = []
    eng = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_order_intent=lambda intent: None,
        on_incident=incidents.append,
        on_status=statuses.append,
    )

    eng._process_bar(np.array([0, 100, 101, 99, 100, 1], dtype=float))
    eng._process_bar(np.array([60_000, 100, 101, 99, 100, 1], dtype=float))

    assert len(broker.submitted) == 1
    assert incidents[0]["type"] == "entry_rejected"
    assert statuses[-1]["entries_halted"] is True


def test_status_open_position_carries_take_profit_and_current_stop():
    candles = _ramp_then_drop(20, 20)
    statuses = []
    eng = LiveEngine(
        ema_cross_graph(),
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        on_status=statuses.append,
    )
    eng.run(ReplayFeed(candles, delay_seconds=0.0), StopSignal())
    open_positions = [s["open_position"] for s in statuses if s["open_position"] is not None]
    assert open_positions, "expected at least one status with an open position"
    pos = open_positions[0]
    assert {"side", "entry_price", "quantity", "current_stop", "take_profit"} <= set(pos)
    assert pos["side"] == "sell"
    assert pos["take_profit"] < pos["entry_price"]
    assert pos["current_stop"] > pos["entry_price"]


def _accepted_ack(session_id, client_order_id, target="binance_sandbox"):
    return BrokerOrderAck(
        session_id=session_id,
        client_order_id=client_order_id,
        exchange_order_id=client_order_id,
        status="accepted",
        target=target,
    )


class _ExternalFillBroker(_RecordingBroker):
    def __init__(self):
        super().__init__()
        del self.position
        self.poll_count = 0
        self.cancel_calls = []
        self.flatten_calls = []

    def poll_fills(self, session_id):
        self.poll_count += 1
        if self.poll_count != 2:
            return []
        intent = self.submitted[0]
        return [
            BrokerFill(
                session_id=session_id,
                client_order_id=intent.client_order_id,
                exchange_order_id="1",
                symbol=intent.symbol,
                side=intent.side,
                status="filled",
                role="entry",
                quantity="1",
                price="100",
                timestamp_ms=60_000,
            )
        ]

    def place_protection(self, intent):
        return [
            _accepted_ack(intent.session_id, intent.stop_client_order_id),
            _accepted_ack(intent.session_id, intent.target_client_order_id),
        ]

    def cancel_all(self, symbol, session_id):
        self.cancel_calls.append((symbol, session_id))
        return [
            BrokerOrderAck(
                session_id=session_id,
                client_order_id=f"cancel-{session_id}",
                exchange_order_id=f"cancel-{session_id}",
                status="canceled",
                target=self.target,
            )
        ]

    def flatten(self, symbol, session_id):
        self.flatten_calls.append((symbol, session_id))
        return _accepted_ack(session_id, f"flatten-{session_id}")


def test_partial_entry_fill_is_never_adopted_and_triggers_containment(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    broker = _ExternalFillBroker()
    incidents = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_incident=incidents.append,
    )
    engine._pending_setup = _FakeSetup()  # noqa: SLF001
    engine._external_entry_pending = True  # noqa: SLF001

    with pytest.raises(RuntimeError, match="partial entry fill"):
        engine._process_external_fill(  # noqa: SLF001
            BrokerFill(
                session_id="session-1",
                client_order_id="kv-entry",
                exchange_order_id="1",
                symbol="BTCUSDT",
                side="buy",
                status="partial",
                role="entry",
                quantity="0.5",
                price="100",
                timestamp_ms=0,
            )
        )

    assert broker.cancel_calls == [("BTCUSDT", "session-1")]
    assert broker.flatten_calls == [("BTCUSDT", "session-1")]
    assert engine._account.snapshot().open_position is None  # noqa: SLF001
    assert incidents[-1]["type"] == "containment_unconfirmed"


def test_terminal_entry_fill_delta_triggers_containment(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    broker = _ExternalFillBroker()
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
    )
    engine._external_entry_pending = True  # noqa: SLF001

    with pytest.raises(RuntimeError, match="terminal entry fill"):
        engine._process_external_fill(  # noqa: SLF001
            BrokerFill(
                session_id="session-1",
                client_order_id="kv-entry",
                exchange_order_id="1",
                symbol="BTCUSDT",
                side="buy",
                status="canceled",
                role="entry",
                quantity="0.5",
                price="100",
                timestamp_ms=0,
            )
        )

    assert broker.cancel_calls == [("BTCUSDT", "session-1")]
    assert broker.flatten_calls == [("BTCUSDT", "session-1")]


def test_unconfirmed_containment_is_visible_in_terminal_status(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class RejectingFlattenBroker(_ExternalFillBroker):
        def flatten(self, symbol, session_id):
            self.flatten_calls.append((symbol, session_id))
            return BrokerOrderAck(
                session_id=session_id,
                client_order_id="flatten",
                exchange_order_id="flatten",
                status="rejected",
                target=self.target,
            )

    broker = RejectingFlattenBroker()
    statuses = []
    incidents = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_status=statuses.append,
        on_incident=incidents.append,
    )
    engine._pending_setup = _FakeSetup()  # noqa: SLF001
    engine._last_entry_client_order_id = "kv-entry"  # noqa: SLF001
    engine._on_open(Fill("entry", "buy", 100.0, 1.0, 0, 0.0))  # noqa: SLF001

    with pytest.raises(RuntimeError, match="containment could not be confirmed"):
        engine._finalize()  # noqa: SLF001

    assert statuses[-1]["terminal"] is True
    assert statuses[-1]["containment_confirmed"] is False
    assert incidents[-1]["type"] == "containment_unconfirmed"


def test_paper_entry_fill_deltas_resize_account_and_protection_once_open(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    broker = _ImmediateFillBroker()
    events = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_event=events.append,
    )
    engine._pending_setup = _FakeSetup()  # noqa: SLF001
    engine._last_entry_client_order_id = "kv-entry"  # noqa: SLF001
    first = Fill(
        "entry",
        "buy",
        100.0,
        1.0,
        0,
        -0.04,
        commission=0.04,
        margin=100.0,
        status="partial",
        cumulative_quantity=1.0,
    )
    second = Fill(
        "entry",
        "buy",
        102.0,
        1.0,
        60_000,
        -0.04,
        commission=0.04,
        margin=102.0,
        status="filled",
        cumulative_quantity=2.0,
    )

    engine._apply_paper_fill(first)  # noqa: SLF001
    engine._apply_paper_fill(second)  # noqa: SLF001

    position = engine._account.snapshot().open_position  # noqa: SLF001
    assert position.quantity == 2.0
    assert position.entry_price == 101.0
    assert engine._account.snapshot().margin_used == 202.0  # noqa: SLF001
    assert [intent.quantity for intent in broker.protection_intents] == ["1.0", "2.0"]
    assert [event["event_type"] for event in events].count("TRADE_OPENED") == 1


def test_final_strategy_callback_receives_aggregate_partial_exit_pnl(monkeypatch):
    class RecordingStrategy(_AlwaysLongStrategy):
        def __init__(self):
            self.closed = []

        def on_close_position(self, trade_id, result):
            self.closed.append((trade_id, result))

    strategy = RecordingStrategy()
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    trades = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        broker=_ImmediateFillBroker(),
        on_trade=trades.append,
    )
    engine._pending_setup = _FakeSetup()  # noqa: SLF001
    engine._last_entry_client_order_id = "kv-entry"  # noqa: SLF001
    engine._on_open(  # noqa: SLF001
        Fill(
            "entry",
            "buy",
            100.0,
            2.0,
            0,
            -0.08,
            commission=0.08,
            margin=200.0,
        )
    )

    engine._apply_paper_fill(  # noqa: SLF001
        Fill(
            "stop_loss",
            "buy",
            90.0,
            1.0,
            60_000,
            -10.04,
            commission=0.04,
            status="partial",
        )
    )
    engine._apply_paper_fill(  # noqa: SLF001
        Fill(
            "stop_loss",
            "buy",
            90.0,
            1.0,
            120_000,
            -10.04,
            commission=0.04,
        )
    )

    assert strategy.closed == [(1, {"pnl": pytest.approx(-20.16)})]
    assert trades[0]["pnl"] == pytest.approx(-20.16)


def test_partial_entry_resize_preserves_a_tightened_dynamic_stop(monkeypatch):
    from decimal import Decimal

    from koval.engine.paper_profile import resolve_paper_profile

    class TighteningStrategy(_SingleEntryStrategy):
        def __init__(self):
            super().__init__(
                entry_price=100.0,
                stop_loss=90.0,
                take_profit=150.0,
                size=2.0,
            )
            self.tightened = False

        def on_sl_update(self, trade_id):
            if self.open_count and not self.tightened:
                self.tightened = True
                return 95.0
            return None

    class RecordingPaperBroker(PaperBroker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.protection_stops = []

        def place_protection(self, intent):
            self.protection_stops.append(float(intent.stop_price))
            return super().place_protection(intent)

    strategy = TighteningStrategy()
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    execution = {
        "version": "paper_ohlcv_realistic_v2",
        "commission_bps": 0.0,
        "spread_bps": 0.0,
        "slippage_bps": 0.0,
    }
    profile = resolve_paper_profile(execution)
    proxy = ExecutionProxyConfig(
        maximum_volume_participation=Decimal("1"),
        entry_remainder_policy="carry",
        latency=ExecutionLatency(),
    )
    broker = RecordingPaperBroker(10_000, profile=profile, execution_proxy=proxy)
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1m",
            initial_capital=10_000,
            execution=execution,
            execution_proxy=proxy,
        ),
        broker=broker,
    )

    engine.run(
        ReplayFeed(
            np.array(
                [
                    [0, 100, 101, 99, 100, 10],
                    [60_000, 100, 101, 99, 100, 1],
                    [120_000, 100, 101, 99, 100, 1],
                ],
                dtype=float,
            )
        ),
        StopSignal(),
    )

    assert broker.protection_stops == [90.0, 95.0]


def test_containment_drains_terminal_entry_delta_before_confirming_flatness(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class SynchronousContainmentBroker(_RecordingBroker):
        def __init__(self):
            super().__init__()
            self.pending = True
            self._fills = []

        def cancel_all(self, symbol, session_id):
            self.pending = False
            self._fills.append(
                BrokerFill(
                    session_id=session_id,
                    client_order_id="kv-entry",
                    exchange_order_id="1",
                    symbol=symbol,
                    side="buy",
                    status="canceled",
                    role="entry",
                    quantity="0.5",
                    price="100",
                    timestamp_ms=0,
                )
            )
            return (
                BrokerOrderAck(
                    session_id=session_id,
                    client_order_id="kv-entry",
                    exchange_order_id="1",
                    status="canceled",
                    target=self.target,
                ),
            )

        def flatten(self, symbol, session_id):
            self.position = None
            self._fills.append(
                BrokerFill(
                    session_id=session_id,
                    client_order_id="kv-flatten",
                    exchange_order_id="2",
                    symbol=symbol,
                    side="buy",
                    status="filled",
                    role="flatten",
                    quantity="0.5",
                    price="99",
                    timestamp_ms=1,
                )
            )
            return BrokerOrderAck(
                session_id=session_id,
                client_order_id="kv-flatten",
                exchange_order_id="2",
                status="filled",
                target=self.target,
            )

        def poll_fills(self, session_id):
            fills, self._fills = self._fills, []
            return fills

    broker = SynchronousContainmentBroker()
    fills = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_fill=fills.append,
    )
    engine._external_entry_pending = True  # noqa: SLF001

    engine._contain_exposure("test")  # noqa: SLF001

    assert [fill["role"] for fill in fills] == ["entry", "flatten"]
    assert engine._containment_confirmed is True  # noqa: SLF001


def test_containment_incident_sanitizes_signed_transport_errors(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class LeakyBroker(_RecordingBroker):
        def cancel_all(self, symbol, session_id):
            raise requests.HTTPError(
                "500 for https://example.test/order?signature=sig-123&api_key=key-123"
            )

        def flatten(self, symbol, session_id):
            raise requests.HTTPError(
                "500 for https://example.test/order?signature=sig-456&api_secret=secret-456"
            )

    incidents = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=LeakyBroker(),
        on_incident=incidents.append,
    )
    engine._external_entry_pending = True  # noqa: SLF001

    engine._contain_exposure("test")  # noqa: SLF001

    persisted = repr(incidents).lower()
    assert "signature" not in persisted
    assert "api_key" not in persisted
    assert "api_secret" not in persisted
    assert "sig-123" not in persisted
    assert "sig-456" not in persisted
    assert "key-123" not in persisted
    assert "secret-456" not in persisted


def test_external_fill_blocks_duplicate_entry_without_optional_position_attribute(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    broker = _ExternalFillBroker()
    eng = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
    )

    eng._process_bar(np.array([0, 100, 101, 99, 100, 1], dtype=float))
    eng._process_bar(np.array([60_000, 100, 101, 99, 100, 1], dtype=float))

    assert len(broker.submitted) == 1


def test_protective_fill_closes_trade_and_cancels_sibling(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class ProtectiveFillBroker(_ExternalFillBroker):
        def poll_fills(self, session_id):
            self.poll_count += 1
            if self.poll_count == 1:
                return []
            intent = self.submitted[0]
            if self.poll_count == 2:
                return [
                    BrokerFill(
                        session_id=session_id,
                        client_order_id=intent.client_order_id,
                        exchange_order_id="1",
                        symbol=intent.symbol,
                        side="buy",
                        status="filled",
                        role="entry",
                        quantity="1",
                        price="100",
                        timestamp_ms=60_000,
                    )
                ]
            if self.poll_count == 3:
                return [
                    BrokerFill(
                        session_id=session_id,
                        client_order_id="stop",
                        exchange_order_id="2",
                        symbol=intent.symbol,
                        side="buy",
                        status="filled",
                        role="stop",
                        quantity="1",
                        price="95",
                        timestamp_ms=120_000,
                        realized_pnl="-5",
                    )
                ]
            return []

    broker = ProtectiveFillBroker()
    events = []
    eng = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_event=events.append,
    )

    for ts in (0, 60_000, 120_000):
        eng._process_bar(np.array([ts, 100, 101, 94, 100, 1], dtype=float))

    assert [event["event_type"] for event in events].count("TRADE_CLOSED") == 1
    assert broker.cancel_calls == [("BTCUSDT", "session-1")]


@pytest.mark.parametrize("status", ["canceled", "expired"])
def test_lost_protective_order_contains_open_exposure(monkeypatch, status):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    broker = _ExternalFillBroker()
    incidents = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_incident=incidents.append,
    )
    engine._pending_setup = _FakeSetup()  # noqa: SLF001
    engine._last_entry_client_order_id = "kv-entry"  # noqa: SLF001
    engine._on_open(Fill("entry", "buy", 100.0, 1.0, 0, 0.0))  # noqa: SLF001

    with pytest.raises(RuntimeError, match="protective order"):
        engine._process_external_fill(  # noqa: SLF001
            BrokerFill(
                session_id="session-1",
                client_order_id="kv-stop",
                exchange_order_id="2",
                symbol="BTCUSDT",
                side="buy",
                status=status,
                role="stop",
                quantity=None,
                price=None,
                timestamp_ms=60_000,
            )
        )

    assert [incident["type"] for incident in incidents] == [
        "protection_lost",
        "containment_unconfirmed",
    ]
    assert broker.flatten_calls == [("BTCUSDT", "session-1")]


def test_protection_failure_contains_open_exposure_and_raises(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class UnprotectedBroker(_ExternalFillBroker):
        def place_protection(self, intent):
            return []

    broker = UnprotectedBroker()
    incidents = []
    eng = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_incident=incidents.append,
    )
    with pytest.raises(RuntimeError, match="protection"):
        eng._process_bar(np.array([0, 100, 101, 99, 100, 1], dtype=float))

    assert broker.cancel_calls == [("BTCUSDT", "session-1")]
    assert broker.flatten_calls == [("BTCUSDT", "session-1")]
    assert "protection_unconfirmed" in [incident["type"] for incident in incidents]


def test_run_contains_unknown_entry_state_and_always_emits_session_end(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class TimeoutBroker(_ExternalFillBroker):
        def submit_entry(self, intent):
            self.submitted.append(intent)
            raise TimeoutError("exchange timeout")

        def poll_fills(self, session_id):
            return []

    broker = TimeoutBroker()
    events = []
    eng = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_event=events.append,
    )

    with pytest.raises(TimeoutError, match="exchange timeout"):
        eng.run(
            ReplayFeed(np.array([[0, 100, 101, 99, 100, 1]], dtype=float)),
            StopSignal(),
        )

    assert broker.cancel_calls == [
        ("BTCUSDT", "session-1"),
        ("BTCUSDT", "session-1"),
    ]
    assert broker.flatten_calls == [
        ("BTCUSDT", "session-1"),
        ("BTCUSDT", "session-1"),
    ]
    assert events[-1]["event_type"] == "SESSION_END"


def _fixed_execution():
    return {
        "version": "paper_ohlcv_fixed_v1",
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 10.0,
    }


def test_fixed_profile_trade_record_uses_actual_fills_and_net_pnl(monkeypatch):
    strategy = _SingleEntryStrategy(entry_price=100.0, stop_loss=90.0, take_profit=120.0, size=2.0)
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    rows = np.array(
        [
            [0, 100, 101, 99, 100, 1],
            [3_600_000, 100, 100.5, 99.5, 100, 1],
            [7_200_000, 85, 86, 84, 85, 1],
        ],
        dtype=float,
    )
    trades, events = [], []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1h",
            initial_capital=10_000.0,
            execution=_fixed_execution(),
        ),
        on_trade=trades.append,
        on_event=events.append,
    )
    engine.run(_RawRowFeed(rows), StopSignal())
    (trade,) = trades
    assert trade["entry_price"] == pytest.approx(100.20)
    assert trade["entry_reference_price"] == 100.0
    assert trade["exit_price"] == pytest.approx(84.83)
    assert trade["exit_reference_price"] == 85.0
    assert trade["commission"] == pytest.approx(0.148024)
    assert trade["gross_price_pnl"] == pytest.approx(-30.74)
    assert trade["pnl"] == pytest.approx(-30.888024)
    assert trade["exit_reason_text"] == "Stop Loss"
    assert trade["execution_costs"]["spread_cost"] == pytest.approx(0.20 + 0.17)
    assert trade["execution_profile"]["version"] == "paper_ohlcv_fixed_v1"
    status = [e for e in events if e["event_type"] == "TRADE_CLOSED"][-1]
    assert status["payload"]["entry_price"] == pytest.approx(100.20)


def test_paper_insufficient_margin_emits_order_rejected_and_keeps_trading(monkeypatch):
    strategy = _ConfiguredSetupStrategy(
        entry_price=100.0, stop_loss=90.0, take_profit=108.0, size=600.0
    )
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    rows = np.array([[0, 100, 101, 99, 100, 1], [3_600_000, 100, 100.5, 99.5, 100, 1]], dtype=float)
    events, status = [], {}
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1h",
            initial_capital=10_000.0,
            execution={**_fixed_execution(), "leverage": 5.0},
        ),
        on_event=events.append,
        on_status=status.update,
    )
    engine.run(_RawRowFeed(rows), StopSignal())
    rejected = [e for e in events if e["event_type"] == "ORDER_REJECTED"]
    assert len(rejected) == 2 and rejected[0]["payload"]["reason"] == "insufficient_margin"
    assert status["entries_halted"] is False


def test_paper_fill_time_insufficient_margin_emits_order_rejected(monkeypatch):
    strategy = _SingleEntryStrategy(entry_price=100.0, stop_loss=90.0, take_profit=120.0, size=99.9)
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda g: strategy)
    rows = np.array([[0, 100, 101, 99, 100, 1], [3_600_000, 100, 100.5, 99.5, 100, 1]], dtype=float)
    events, trades, status = [], [], {}
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1h",
            initial_capital=10_000.0,
            execution=_fixed_execution(),
        ),
        on_event=events.append,
        on_trade=trades.append,
        on_status=status.update,
    )
    engine.run(_RawRowFeed(rows), StopSignal())
    rejected = [e for e in events if e["event_type"] == "ORDER_REJECTED"]
    assert len(rejected) == 1
    assert rejected[0]["payload"]["reason"] == "insufficient_margin"
    assert status["entries_halted"] is False
    assert trades == []


def test_paper_fill_time_rejection_preserves_non_margin_reason_code(monkeypatch):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )
    events = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        broker=_ImmediateFillBroker(),
        on_event=events.append,
    )
    engine._pending_setup = _FakeSetup()  # noqa: SLF001

    engine._on_paper_entry_rejected(  # noqa: SLF001
        "instrument_constraint: order is below the minimum quantity"
    )

    rejected = [event for event in events if event["event_type"] == "ORDER_REJECTED"]
    assert rejected[0]["payload"]["reason"] == "instrument_constraint"


def test_on_open_threads_real_margin_into_the_account_snapshot(monkeypatch):
    strategy = _SingleEntryStrategy(
        entry_price=100.0, stop_loss=90.0, take_profit=108.0, size=200.0
    )
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda g: strategy)
    rows = np.array(
        [
            [0, 100, 101, 99, 100, 1],
            [3_600_000, 100, 100.5, 99.5, 100, 1],
            [7_200_000, 100, 100.5, 99.5, 100, 1],
        ],
        dtype=float,
    )
    statuses = []
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1h",
            initial_capital=10_000.0,
            execution={**_fixed_execution(), "leverage": 5.0},
        ),
        on_status=lambda s: statuses.append(dict(s)),
    )
    engine.run(_RawRowFeed(rows), StopSignal())

    open_statuses = [s for s in statuses if s["open_position"] is not None]
    assert open_statuses, "expected at least one status while the position was open"
    live = open_statuses[-1]
    assert live["margin_used"] == pytest.approx(200 * 100.2 / 5, rel=1e-9)
    assert live["free_margin"] == pytest.approx(
        live["metrics"]["equity"] - live["margin_used"], rel=1e-6
    )


def test_non_paper_insufficient_margin_ack_halts_instead_of_soft_rejecting(monkeypatch):
    """The soft 'keep trading' path for an affordability rejection is the paper
    broker's alone. A sandbox ack carrying the same reason string must still
    halt entries and raise an incident, not be swallowed as an event."""
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda graph: _AlwaysLongStrategy()
    )

    class SandboxRejectedBroker(_RecordingBroker):
        def submit_entry(self, intent):
            self.submitted.append(intent)
            return BrokerOrderAck(
                session_id=intent.session_id,
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                status="rejected",
                target=self.target,
                metadata={"reason": "insufficient_margin"},
            )

    broker = SandboxRejectedBroker()
    assert broker.target == "binance_sandbox"
    incidents, events, statuses = [], [], []
    eng = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="session-1",
        broker=broker,
        on_incident=incidents.append,
        on_event=events.append,
        on_status=statuses.append,
    )

    eng._process_bar(np.array([0, 100, 101, 99, 100, 1], dtype=float))  # noqa: SLF001
    eng._process_bar(np.array([60_000, 100, 101, 99, 100, 1], dtype=float))  # noqa: SLF001

    assert incidents[0]["type"] == "entry_rejected"
    assert statuses[-1]["entries_halted"] is True
    assert not [e for e in events if e["event_type"] == "ORDER_REJECTED"]


class _DeferredEntryBroker(_RecordingBroker):
    """Accepts a working entry, then delivers its fill only after ``arm()`` -
    the way a limit/stop entry fills seconds after placement, between bar
    closes. Confirms containment at finalize so ``run()`` completes cleanly."""

    def __init__(self):
        super().__init__()
        self._queued = []
        self._entry = None
        self.protection_intents = []

    def submit_entry(self, intent):
        self.submitted.append(intent)
        self._entry = BrokerFill(
            session_id=intent.session_id,
            client_order_id=intent.client_order_id,
            exchange_order_id="e1",
            symbol=intent.symbol,
            side=intent.side,
            status="filled",
            role="entry",
            quantity=intent.quantity,
            price=intent.price,
            timestamp_ms=0,
        )
        return BrokerOrderAck(
            session_id=intent.session_id,
            client_order_id=intent.client_order_id,
            exchange_order_id="e1",
            status="accepted",
            target=self.target,
        )

    def arm(self):
        if self._entry is not None:
            self._queued.append(self._entry)
            self._entry = None

    def poll_fills(self, session_id):
        out, self._queued = self._queued, []
        return out

    def place_protection(self, intent):
        self.protection_intents.append(intent)
        return _ImmediateFillBroker.place_protection(self, intent)

    def cancel_all(self, symbol, session_id):
        return [
            BrokerOrderAck(
                session_id=session_id,
                client_order_id=f"cancel-{session_id}",
                exchange_order_id=None,
                status="canceled",
                target=self.target,
            )
        ]

    def flatten(self, symbol, session_id):
        self._queued.append(
            BrokerFill(
                session_id=session_id,
                client_order_id=f"flat-{session_id}",
                exchange_order_id="f1",
                symbol=symbol,
                side="sell",
                status="filled",
                role="flatten",
                quantity="1.0",
                price="100.0",
                timestamp_ms=0,
                realized_pnl="0.0",
            )
        )
        return BrokerOrderAck(
            session_id=session_id,
            client_order_id=f"flat-{session_id}",
            exchange_order_id="f1",
            status="filled",
            target=self.target,
        )


def test_polling_feed_between_bars_hook_drives_watch_orders_to_protect_a_fill(monkeypatch):
    """The CHANGELOG claims PollingFeed(between_bars=...) + LiveEngine.watch_orders()
    protect a working entry within seconds of its fill. Prove the two compose:
    the feed's between-bars hook, wired to watch_orders, delivers the entry fill
    and places protection while only bar 0 has been processed - not on the next
    bar close."""
    from koval.engine.live_feed import PollingFeed

    strategy = _SingleEntryStrategy(
        entry_type="stop", entry_price=101.0, stop_loss=95.0, take_profit=110.0
    )
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda g: strategy)

    tf_ms = 60_000
    candles = np.array(
        [
            [0, 100, 101, 99, 100, 1],
            [tf_ms, 100, 101, 99, 100, 1],
            [2 * tf_ms, 100, 101, 99, 100, 1],
        ],
        dtype=float,
    )

    class _Adapter:
        def fetch_ohlcv(self, symbol, timeframe, start_ms, end_ms):
            return candles

    broker = _DeferredEntryBroker()
    events = []
    stop = StopSignal()
    now = [tf_ms]  # bar 0 is closed; bar 1 is still forming
    engine = LiveEngine(
        {"blocks": [], "connections": []},
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        session_id="s1",
        broker=broker,
        on_event=events.append,
    )

    seen = {"n": 0, "bar_index_at_protection": None}

    def between_bars():
        seen["n"] += 1
        if seen["n"] == 1:
            broker.arm()
        engine.watch_orders()
        if broker.protection_intents and seen["bar_index_at_protection"] is None:
            seen["bar_index_at_protection"] = engine._bar_index  # noqa: SLF001
            stop.set()

    feed = PollingFeed(
        _Adapter(),
        symbol="BTCUSDT",
        timeframe="1m",
        clock=lambda: now[0],
        poll_seconds=1.0,
        between_bars=between_bars,
        between_bars_seconds=0.001,
    )

    engine.run(feed, stop)

    assert broker.submitted, "a working entry was placed on bar 0"
    assert broker.protection_intents, "watch_orders placed protection between bars"
    assert seen["bar_index_at_protection"] == 1, "protection came from the hook, not the bar loop"
    assert any(e["event_type"] == "TRADE_OPENED" for e in events)
