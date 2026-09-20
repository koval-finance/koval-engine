"""Prepared scalars accelerate facts while every graph state transition still runs."""

from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pytest

from koval.strategy.graph.indicators import PreparedIndicators
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.registry import get_node
from koval.strategy.graph.strategy import build_graph_strategy
from koval.strategy.helpers._math import _atr

CASES = [
    ("fact.ema_cross", {"fast": 5, "slow": 14}),
    ("fact.rsi_cross", {"period": 14, "level": 50, "direction": "cross_up"}),
    ("fact.rsi_cross", {"period": 14, "level": 50, "direction": "cross_down"}),
    ("state.trend_bias", {"period": 14}),
    ("state.volatility_regime", {"period": 14, "min_atr_pct": 3}),
    ("policy.ema_trend", {"period": 14, "direction": "bullish"}),
    ("policy.ema_trend", {"period": 14, "direction": "bearish"}),
    ("policy.atr_volatility", {"period": 14, "min_atr_pct": 3}),
]


def candles(count=140):
    rng = np.random.default_rng(47)
    closes = 100 + rng.normal(0, 3, count).cumsum()
    return np.column_stack(
        (np.arange(count) * 60000, closes, closes + 2, closes - 2, closes, np.ones(count))
    )


def context(rows, end, window=31):
    history = rows[max(0, end - window) : end]
    stamp, op, high, low, close, vol = rows[end - 1]
    return BarContext(
        close,
        high,
        low,
        op,
        vol,
        end,
        int(stamp),
        closes=history[:, 4],
        highs=history[:, 2],
        lows=history[:, 3],
    )


@pytest.mark.parametrize("node_type,params", CASES)
def test_prepared_node_outputs_and_state_equal_every_scalar_step(node_type, params):
    graph = {"blocks": [{"id": "node", "type": node_type, "params": params}], "connections": []}
    rows = candles()
    prepared = PreparedIndicators.build(graph, rows, 31)
    spec = get_node(node_type)
    normal = spec.factory(spec.params_schema(**params))
    fast = spec.factory(spec.params_schema(**params))
    normal_state, fast_state = {}, {}
    for end in range(1, len(rows) + 1):
        ctx = context(rows, end)
        values = prepared.at(ctx.timestamp_ms, len(ctx.closes))
        assert values
        expected = normal(ctx, {}, normal_state)
        # Any scalar fallback after preparation defeats the optimization.
        with (
            patch("koval.strategy.graph.indicators._ema", side_effect=AssertionError("slow EMA")),
            patch("koval.strategy.graph.indicators._rsi", side_effect=AssertionError("slow RSI")),
            patch("koval.strategy.graph.indicators._atr", side_effect=AssertionError("slow ATR")),
        ):
            actual = fast(replace(ctx, indicators=values), {}, fast_state)
        assert actual == expected
        assert fast_state == normal_state


def test_lookup_rejects_wrong_timestamp_or_window_and_returns_only_current_scalars():
    graph = {"blocks": [{"id": "ema", "type": "fact.ema_cross", "params": {}}]}
    table = PreparedIndicators.build(graph, candles(), 31)
    assert table.at(123, 31) is None
    assert table.at(60_000 * 80, 30) is None
    assert table.at(-60_000, 1) is None
    assert table.at(60_000 * 999, 31) is None
    values = table.at(60_000 * 80, 31)
    assert set(values) == {("ema", 9), ("ema", 21)}
    assert all(isinstance(pair, tuple) and len(pair) == 2 for pair in values.values())
    values.clear()
    assert table.at(60_000 * 80, 31)


def test_graph_prepares_only_requested_indicators_and_keeps_instances_isolated():
    graph = {
        "blocks": [{"id": "trend", "type": "state.trend_bias", "params": {}}],
        "connections": [],
    }
    cls = build_graph_strategy(graph)
    normal, fast = cls(), cls()
    rows = candles()
    fast.prepare_backtest(rows, history_bars=31)
    for strategy in (normal, fast):
        ctx = context(rows, 80)
        for key in ("closes", "highs", "lows", "timestamp_ms", "bar_index"):
            setattr(strategy, key, getattr(ctx, key))
    assert normal._ctx().indicators is None
    assert set(fast._ctx().indicators) == {("ema", 50)}


@pytest.mark.parametrize("invalid", ["nonfinite", "duplicate", "unordered"])
def test_untrusted_feed_disables_preparation(invalid):
    rows = candles()
    if invalid == "nonfinite":
        rows[10, 4] = np.nan
    elif invalid == "duplicate":
        rows[10, 0] = rows[9, 0]
    else:
        rows[[9, 10]] = rows[[10, 9]]
    graph = {"blocks": [{"id": "ema", "type": "fact.ema_cross", "params": {}}]}
    assert PreparedIndicators.build(graph, rows, 31).at(60_000 * 80, 31) is None


def test_graph_without_supported_nodes_keeps_fallback():
    graph = {"blocks": [{"id": "bar", "type": "fact.every_bar", "params": {}}]}
    assert PreparedIndicators.build(graph, candles(), 31).at(60_000 * 80, 31) is None


@pytest.mark.parametrize("dtype", [np.float32, np.int64])
def test_prepared_atr_uses_the_adapters_float64_price_arithmetic(dtype):
    rows = candles()
    rows[:, 2] *= 5.3127
    rows[:, 3] *= 0.1321
    rows = rows.astype(dtype)
    graph = {"blocks": [{"id": "atr", "type": "state.volatility_regime", "params": {}}]}
    table = PreparedIndicators.build(graph, rows, 31)
    for end in range(15, len(rows) + 1):
        history = rows[max(0, end - 31) : end].astype(float)
        expected = _atr(history[:, 2], history[:, 3], history[:, 4], 14)
        assert table.at(int(rows[end - 1, 0]), len(history))["atr", 14][1] == expected
