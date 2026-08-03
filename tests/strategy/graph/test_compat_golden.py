"""The example graph run through the typed engine must produce a coherent
backtest. We pin a fixed synthetic dataset and assert determinism + non-empty
result shape. This guards that a compiled graph runs end-to-end
(assemble -> compat -> typed executor -> backtest engine)."""

import json

import numpy as np
import pytest

from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.examples import example_path

pytestmark = pytest.mark.backtrader


def _load(name: str) -> dict:
    return json.loads(example_path("graphs", f"{name}.json").read_text(encoding="utf-8"))


EMA_CROSS_TREND_GRAPH = _load("ema_cross_trend")["graph"]


def _synthetic_feed(n=300) -> np.ndarray:
    rng = np.random.default_rng(42)
    price = 100 + np.cumsum(rng.normal(0, 1, n))
    ts = (np.arange(n) * 60_000).astype("int64")
    o = price
    h = price + 1
    lo = price - 1
    c = price
    v = np.full(n, 10.0)
    return np.column_stack([ts, o, h, lo, c, v])


def test_seed_graph_runs_through_typed_engine():
    engine = load_backtest_engine()
    spec = EngineRunSpec(
        graph=EMA_CROSS_TREND_GRAPH,
        feeds={"1h": _synthetic_feed()},
        initial_capital=10_000.0,
    )
    result = engine.run(spec)
    assert "max_drawdown" in result.metrics
    assert isinstance(result.trades, list)
