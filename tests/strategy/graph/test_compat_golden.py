"""The example graph run through the typed engine must produce a coherent
backtest. We pin a fixed synthetic dataset and assert determinism + non-empty
result shape. This guards that a compiled graph runs end-to-end
(assemble -> compat -> typed executor -> backtest engine)."""

import json

import numpy as np
import pytest

from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.engine.run_identity import content_sha256
from koval.examples import example_path
from koval.strategy.graph.compat import compile_legacy
from koval.strategy.graph.registry import get_node


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


@pytest.mark.backtrader
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


@pytest.mark.parametrize("overrides", [{}, {"take_profit_mode": "bracket"}])
def test_default_bracket_preserves_released_parameter_dump_and_hash(overrides):
    params = get_node("exec.order_constructor").params_schema(**overrides)
    assert params.take_profit_mode == "bracket"
    assert params.model_dump() == {
        "sl_pct": 2.0,
        "risk_reward": 2.0,
        "tp_pct": None,
        "risk_pct": 1.0,
        "leverage": 1.0,
        "entry_type": "market",
    }
    assert params.model_dump_json() == (
        '{"sl_pct":2.0,"risk_reward":2.0,"tp_pct":null,"risk_pct":1.0,'
        '"leverage":1.0,"entry_type":"market"}'
    )
    assert (
        content_sha256(params.model_dump())
        == "00c14755f4010a5e12790da7ea66662b01f88d4815878bb17b045cdf489e15e9"
    )


def test_legacy_compilation_and_normalized_graph_keep_released_hashes():
    graph = compile_legacy(EMA_CROSS_TREND_GRAPH)
    assert (
        content_sha256(graph) == "d6cc489b564ae9dde8f28339159c8db63257f6b9b358d08bc2a256cb881f672d"
    )
    for block in graph["blocks"]:
        block["params"] = get_node(block["type"]).params_schema(**block["params"]).model_dump()
    assert (
        content_sha256(graph) == "1c2ef35db4ece6a7253c9b2fccf9e4b602bd8a53d386965368a6a4a6ea126dfb"
    )


def test_disabled_tp_serializes_explicitly_and_changes_graph_hash():
    schema = get_node("exec.order_constructor").params_schema
    disabled = schema(take_profit_mode="disabled")
    assert disabled.model_dump()["take_profit_mode"] == "disabled"
    assert schema.model_validate_json(disabled.model_dump_json()) == disabled
    assert content_sha256(disabled.model_dump()) != content_sha256(schema().model_dump())
