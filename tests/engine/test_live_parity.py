"""One brain, two runtimes: the LiveEngine and the backtest engine must emit the
same SIGNAL_DETECTED stream (decision-time, pre-fill) on identical bars. Fills
may differ (paper sim vs Backtrader broker) - that is expected and out of scope
for this assertion."""

import numpy as np
import pytest

from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from tests.engine.live_fixtures import ema_cross_graph

pytestmark = pytest.mark.backtrader


def _ramp_then_drop(n_up: int, n_down: int) -> np.ndarray:
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


def _signal_dirs_live(graph, candles):
    events = []
    LiveEngine(
        graph,
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10_000.0),
        on_event=events.append,
    ).run(ReplayFeed(candles, delay_seconds=0.0), StopSignal())
    return [e["payload"]["direction"] for e in events if e["event_type"] == "SIGNAL_DETECTED"]


def _signal_dirs_backtest(graph, candles):
    events = []
    spec = EngineRunSpec(graph=graph, feeds={"1m": candles}, initial_capital=10_000.0)
    load_backtest_engine().run(spec, on_event=events.append)
    return [e["payload"]["direction"] for e in events if e.get("event_type") == "SIGNAL_DETECTED"]


def test_live_and_backtest_emit_same_signal_directions():
    candles = _ramp_then_drop(25, 25)
    graph = ema_cross_graph()
    live = _signal_dirs_live(graph, candles)
    bt = _signal_dirs_backtest(graph, candles)
    assert live, "expected at least one signal"
    assert live == bt
