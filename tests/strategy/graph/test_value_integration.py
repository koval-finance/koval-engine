"""Public graph/strategy/series boundaries for historical numeric values."""

import json
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from koval.engine.backtest_engine import (
    EngineRunSpec,
    ExecutionCapabilities,
    ProtocolVersionError,
    negotiate_execution_capabilities,
)
from koval.strategy.block_assembler import GraphValidationError, assemble_from_graph
from koval.strategy.graph.evidence import DecisionRecorder
from koval.strategy.graph.executor import GraphExecutor
from koval.strategy.graph.indicators import PreparedIndicators
from koval.strategy.graph.registry import get_node
from koval.strategy.graph.series import strategy_indicator_series
from tests.strategy.graph.test_values import context


def edge(source, source_port, target, target_port):
    return {"from": source, "from_port": source_port, "to": target, "to_port": target_port}


def graph():
    return {
        "blocks": [
            {"id": "price", "type": "source.value", "params": {"component": "close"}},
            {
                "id": "level",
                "type": "source.value",
                "params": {"component": "channel.upper", "period": 2},
            },
            {"id": "cross", "type": "fact.cross", "params": {"direction": "up"}},
            {"id": "agreement", "type": "interp.confluence_and", "params": {}},
            {"id": "gate", "type": "interp.direction_gate", "params": {"allow_short": False}},
            {"id": "order", "type": "exec.order_constructor", "params": {}},
            {"id": "ma", "type": "source.value", "params": {"component": "ema", "period": 2}},
            {"id": "below", "type": "fact.compare", "params": {"operator": "lt"}},
            {"id": "exit_policy", "type": "policy.from_condition", "params": {}},
            {"id": "exit", "type": "exec.position_exit", "params": {}},
        ],
        "connections": [
            edge("price", "value", "cross", "left"),
            edge("level", "value", "cross", "right"),
            edge("cross", "event", "agreement", "events"),
            edge("agreement", "agreement", "gate", "agreement"),
            edge("gate", "intent", "order", "intent"),
            edge("price", "value", "below", "left"),
            edge("ma", "value", "below", "right"),
            edge("below", "condition", "exit_policy", "condition"),
            edge("exit_policy", "policy", "exit", "condition"),
        ],
    }


def test_public_executor_entry_exit_and_recorded_evidence():
    executor = GraphExecutor.build(graph())
    for closes in ((10, 10, 10), (10, 10, 10, 12)):
        result = executor.step(context(closes, evidence=DecisionRecorder(), account_value=10000))
    assert len(result.orders) == 1 and result.orders[0].side == "buy"
    assert result.position_exits == []
    assert result.decision_context["status"] == "recorded"
    source = next(n for n in result.decision_context["nodes"] if n["node_id"] == "price")
    assert source["values"]["value"] == source["outputs"]["value"]["value"] == 12.0
    result = executor.step(
        context(
            (10, 10, 10, 12, 8),
            evidence=DecisionRecorder(),
            position_size=1,
            position_direction="long",
            position_id=42,
        )
    )
    assert result.orders == []
    assert len(result.position_exits) == 1 and result.position_exits[0].position_id == 42
    evidence = result.position_exits[0].decision_context
    assert evidence["status"] == "recorded"
    assert {n["node_id"] for n in evidence["nodes"]} == {
        "price",
        "ma",
        "below",
        "exit_policy",
        "exit",
    }


@pytest.mark.parametrize(
    "change",
    ["right_and_literal", "missing_literal", "between_right", "atr_missing", "percent_atr"],
)
def test_parameter_dependent_connections_rejected_at_build(change):
    g = {
        "blocks": [
            {"id": "a", "type": "source.value", "params": {"component": "close"}},
            {"id": "b", "type": "fact.compare", "params": {"operator": "gt"}},
        ],
        "connections": [edge("a", "value", "b", "left")],
    }
    if change != "missing_literal":
        g["connections"].append(edge("a", "value", "b", "right"))
    if change == "right_and_literal":
        g["blocks"][1]["params"] |= {"literal": 1.0, "unit": "price_quote_per_base"}
    if change == "between_right":
        g["blocks"][1]["params"] = {
            "operator": "between",
            "lower": 1.0,
            "upper": 2.0,
            "unit": "price_quote_per_base",
        }
    if change in {"atr_missing", "percent_atr"}:
        g["blocks"][1] = {
            "id": "b",
            "type": "fact.distance",
            "params": {"mode": "atr" if change == "atr_missing" else "percent"},
        }
        g["connections"] = [edge("a", "value", "b", "price"), edge("a", "value", "b", "level")]
        if change == "percent_atr":
            g["connections"].append(edge("a", "value", "b", "atr"))
    with pytest.raises(GraphValidationError):
        GraphExecutor.build(g)


def test_graph_rejects_direct_event_to_exit_and_fan_in():
    g = graph()
    g["connections"][-1] = edge("cross", "event", "exit", "condition")
    with pytest.raises(GraphValidationError, match="type"):
        GraphExecutor.build(g)
    g = graph()
    g["connections"].append(edge("ma", "value", "cross", "left"))
    with pytest.raises(GraphValidationError, match="at most 1"):
        GraphExecutor.build(g)


def test_values_require_explicit_runtime_capability_before_any_run():
    g = graph()
    strategy = assemble_from_graph(g)
    assert strategy.required_execution_capabilities() == {"numeric_values_v1", "position_exit_v1"}
    spec = EngineRunSpec(g, {}, 10000)
    with pytest.raises(ProtocolVersionError, match="numeric_values_v1"):
        negotiate_execution_capabilities(spec, ExecutionCapabilities((1,), ("position_exit_v1",)))
    assert (
        set(
            negotiate_execution_capabilities(
                spec, ExecutionCapabilities((1,), ("position_exit_v1", "numeric_values_v1"))
            ).features
        )
        == strategy.required_execution_capabilities()
    )


@pytest.mark.parametrize(
    "component",
    [
        "close",
        "volume",
        "ema",
        "atr",
        "bb.middle",
        "bb.upper",
        "bb.width_pct",
        "bb.zscore",
        "channel.upper",
    ],
)
def test_source_direct_prepared_series_and_evidence_equality(component):
    from koval.strategy.graph.values import ValueSpecV1, value_at

    params = {"component": component}
    if component not in {"close", "volume"}:
        params["period"] = 3
    params["offset_bars"] = 1
    g = {"blocks": [{"id": "source", "type": "source.value", "params": params}], "connections": []}
    closes = np.array([10, 10, 10, 14, 9, 12, 15, 11.0])
    rows = np.column_stack(
        (
            np.arange(len(closes)) * 60000,
            closes,
            closes + 1,
            closes - 1,
            closes,
            np.full(len(closes), 10.0),
        )
    )
    prepared = PreparedIndicators.build(g, rows, 5)
    series = strategy_indicator_series(g, rows, timeframe="1m", symbol="BTCUSDT", history_bars=5)[0]
    executor = GraphExecutor.build(g)
    for end in range(1, len(rows) + 1):
        ctx = context(tuple(closes[:end]), evidence=DecisionRecorder())
        if end > 5:
            ctx = replace(
                ctx,
                closes=ctx.closes[-5:],
                highs=ctx.highs[-5:],
                lows=ctx.lows[-5:],
                opens=ctx.opens[-5:],
                volumes=ctx.volumes[-5:],
                history_start_ms=(end - 5) * 60000,
            )
        expected = value_at(ctx, ValueSpecV1(**params))
        actual = executor.step(
            replace(ctx, indicators=prepared.at(ctx.timestamp_ms, len(ctx.closes)))
        ).entities_by_node["source"]["value"]
        assert actual.model_dump(exclude={"source_node_id"}) == expected.model_dump(
            exclude={"source_node_id"}
        )
        point = series["points"][end - 1]
        assert point["value"] == expected.value
        assert point["readiness"] == expected.readiness
        assert point["as_of_ms"] == expected.as_of_ms
        observed = ctx.evidence.nodes[0]
        assert observed["outputs"]["value"] == actual.model_dump()
        assert observed["values"]["value"] == expected.value


def test_history_budget_is_explicit_and_refuses_unsupported_host_window():
    from koval.strategy.graph.values import graph_required_history_bars

    g = {
        "blocks": [
            {
                "id": "value",
                "type": "source.value",
                "params": {"component": "channel.upper", "period": 400, "offset_bars": 20},
            }
        ]
    }
    assert graph_required_history_bars(g) == 421
    strategy = assemble_from_graph(g)
    with pytest.raises(ValueError, match="history"):
        strategy.prepare_backtest(np.empty((0, 6)), history_bars=400)


def test_public_strategy_restore_same_bar_is_safe_and_next_cross_is_once():
    g = graph()
    first, restored = assemble_from_graph(g), assemble_from_graph(g)
    for strategy in (first, restored):
        strategy.config = {"symbol": "BTCUSDT", "timeframe": "1m"}

    def inject(strategy, closes):
        ctx = context(closes, account_value=10000)
        for key, val in vars(ctx).items():
            if key not in {"position_id", "timeframe", "indicators", "evidence", "account"}:
                setattr(strategy, key, val)

    inject(first, (10, 10, 10))
    first.on_bar()
    state = json.loads(json.dumps(first.export_graph_state()))
    restored.restore_graph_state(state)
    inject(restored, (10, 10, 10))
    assert not restored.should_long()
    for strategy in (first, restored):
        inject(strategy, (10, 10, 10, 12))
        assert strategy.should_long()
        assert strategy.go_long().entry_price == 12
    assert first.go_long().decision_context == restored.go_long().decision_context


def test_graph_build_and_evaluation_do_not_rewrite_saved_input():
    g = graph()
    saved = deepcopy(g)
    GraphExecutor.build(g).step(context())
    assert g == saved


def test_legacy_entities_have_no_scalar_defaults():
    from koval.strategy.graph.entities import MarketEvent, PolicyDecision

    assert "readiness" not in PolicyDecision.model_fields
    assert "readiness" not in MarketEvent.model_fields


def test_bollinger_return_uses_same_saved_primitive_graph_with_fixed_risk():
    g = graph()
    g["blocks"][1]["params"] = {"component": "bb.lower", "period": 3, "std_dev": 1.0}
    executor = GraphExecutor.build(g)
    assert not executor.step(context((10, 10, 1), account_value=10000)).orders
    result = executor.step(context((10, 10, 1, 7), account_value=10000))
    assert len(result.orders) == 1 and result.orders[0].side == "buy"
    assert result.orders[0].stop_price == pytest.approx(7 * 0.98)
    assert result.orders[0].target_price is not None
    assert result.position_exits == []


def test_condition_and_source_fan_out_and_entry_policy_bridge():
    g = graph()
    g["blocks"] += [
        {
            "id": "permit",
            "type": "fact.compare",
            "params": {"operator": "gt", "literal": 5.0, "unit": "price_quote_per_base"},
        },
        {"id": "entry_policy", "type": "policy.from_condition", "params": {}},
    ]
    g["connections"] += [
        edge("price", "value", "permit", "left"),
        edge("permit", "condition", "entry_policy", "condition"),
        edge("entry_policy", "policy", "gate", "policies"),
    ]
    executor = GraphExecutor.build(g)
    executor.step(context((10, 10, 10), account_value=10000))
    assert executor.step(context((10, 10, 10, 12), account_value=10000)).orders
    # One condition can feed multiple bridges and consumers.
    g["connections"][-1] = edge("below", "condition", "entry_policy", "condition")
    g["connections"].pop(-2)
    GraphExecutor.build(g)


@pytest.mark.parametrize("target,port", [("cross", "left"), ("below", "right")])
def test_static_numeric_units_reject_mismatched_sources(target, port):
    g = graph()
    g["blocks"].append({"id": "volume", "type": "source.value", "params": {"component": "volume"}})
    g["connections"] = [c for c in g["connections"] if (c["to"], c["to_port"]) != (target, port)]
    g["connections"].append(edge("volume", "value", target, port))
    with pytest.raises(GraphValidationError, match="unit identity"):
        GraphExecutor.build(g)


def test_source_schemas_roundtrip_defaults():
    for kind, params in [
        ("source.value", {"component": "bb.upper"}),
        ("fact.compare", {"operator": "gt"}),
        ("fact.cross", {}),
        ("fact.distance", {"mode": "atr"}),
        ("policy.from_condition", {}),
    ]:
        schema = get_node(kind).params_schema
        value = schema(**params)
        assert schema.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize("terminal", [True, False])
def test_numeric_graph_live_refuses_before_broker_or_feed(terminal):
    from unittest.mock import Mock

    from koval.engine.live_engine import LiveEngine, LiveEngineConfig

    g = graph()
    if not terminal:
        g = {"blocks": [g["blocks"][0]], "connections": []}
    feed = Mock()
    feed.__iter__ = Mock(side_effect=AssertionError("feed touched"))
    broker = Mock()
    with pytest.raises(ValueError, match="numeric_values_v1"):
        LiveEngine(
            g,
            LiveEngineConfig(symbol="BTCUSDT", timeframe="1m", initial_capital=10000),
            broker=broker,
        )
    assert broker.mock_calls == []
    feed.__iter__.assert_not_called()


def test_restored_same_bar_does_not_advance_legacy_temporal_state():
    g = {
        "blocks": [
            {"id": "event", "type": "fact.every_bar", "params": {}},
            {
                "id": "chain",
                "type": "interp.event_aggregator",
                "params": {"event_sequence": ["every_bar", "every_bar"]},
            },
        ],
        "connections": [edge("event", "event", "chain", "events")],
    }
    first, restored = assemble_from_graph(g), assemble_from_graph(g)

    def inject(strategy, index):
        strategy.bar_index = index
        strategy.timestamp_ms = index * 60000

    inject(first, 2)
    first.on_bar()
    saved = json.loads(json.dumps(first.export_graph_state()))
    restored.restore_graph_state(saved)
    inject(restored, 2)
    restored.on_bar()
    assert restored.export_graph_state() == saved
    assert restored._result.entities_by_node.get("chain", {}).get("candidate") is None
    for strategy in (first, restored):
        inject(strategy, 3)
        strategy.on_bar()
    assert first._result.entities_by_node == restored._result.entities_by_node
    assert first.export_graph_state() == restored.export_graph_state()


def test_distance_cross_rearms_when_underlying_formula_changes_on_restore():
    g = {
        "blocks": [
            {"id": "price", "type": "source.value", "params": {"component": "close"}},
            {"id": "ma", "type": "source.value", "params": {"component": "ema", "period": 3}},
            {"id": "distance", "type": "fact.distance", "params": {"mode": "percent"}},
            {"id": "zero", "type": "fact.distance", "params": {"mode": "percent"}},
            {"id": "cross", "type": "fact.cross", "params": {"direction": "up"}},
        ],
        "connections": [
            edge("price", "value", "distance", "price"),
            edge("ma", "value", "distance", "level"),
            edge("price", "value", "zero", "price"),
            edge("price", "value", "zero", "level"),
            edge("distance", "value", "cross", "left"),
            edge("zero", "value", "cross", "right"),
        ],
    }
    first = GraphExecutor.build(g)
    first.step(context((110, 10, 10)))
    saved = json.loads(json.dumps(first.state._states))
    g["blocks"][1]["params"]["period"] = 2
    restored = GraphExecutor.build(g)
    restored.state._states = saved
    result = restored.step(context((110, 10, 10, 35)))
    assert result.entities_by_node["cross"]["event"] is None
