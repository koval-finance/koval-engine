import numpy as np
import pytest

import koval.strategy.nodes  # noqa: F401
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.block_assembler import assemble_from_graph
from koval.strategy.presets.sweep_choch_reversal import build_sweep_choch_reversal_graph


def _ohlcv(n: int = 120) -> np.ndarray:
    closes = np.concatenate([np.linspace(100.0, 140.0, n // 2), np.linspace(140.0, 110.0, n // 2)])
    highs = closes + 1.0
    lows = closes - 1.0
    opens = np.concatenate([[closes[0]], closes[:-1]])
    volumes = np.full(n, 1000.0)
    ts = (np.arange(n, dtype=np.int64) + 1) * 3_600_000
    return np.column_stack([ts, opens, highs, lows, closes, volumes])


def test_preset_graph_is_a_valid_native_graph():
    graph = build_sweep_choch_reversal_graph()
    assert {"blocks", "connections"} <= set(graph)
    types = {b["type"] for b in graph["blocks"]}
    assert "interp.event_aggregator" in types
    assert "interp.signal_emitter" in types


def test_preset_assembles_through_native_path():
    strat = assemble_from_graph(build_sweep_choch_reversal_graph())
    assert isinstance(strat, DeclarativeStrategy)


@pytest.mark.backtrader
def test_preset_runs_through_engine_and_returns_result():
    spec = EngineRunSpec(
        graph=build_sweep_choch_reversal_graph(),
        feeds={"1h": _ohlcv()},
        initial_capital=10_000.0,
    )
    result = load_backtest_engine().run(spec)
    # The native preset assembles and runs end-to-end through the real engine
    # (a well-formed result with the standard metrics). A *trade* with reasoning
    # is proven deterministically in
    # tests/strategy/graph/test_interp_full_engine_golden.py — real detectors on
    # smooth synthetic data cannot be relied on to fire a CHoCH.
    assert "total_trades" in result.metrics
    assert isinstance(result.trades, list)
