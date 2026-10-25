"""Historical condition arithmetic, history and public execution contract regressions."""

import importlib
import json
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest
from pydantic import ValidationError

from koval.strategy.block_assembler import GraphValidationError, assemble_from_graph
from koval.strategy.graph.evidence import DecisionRecorder
from koval.strategy.graph.executor import GraphExecutor
from koval.strategy.graph.indicators import PreparedIndicators
from koval.strategy.graph.series import strategy_indicator_series
from koval.strategy.graph.values import VALUE_FORMULA_VERSION, ValueSpecV1, value_at
from koval.strategy.helpers.filters.trend import adx_filter
from tests.strategy.graph.test_value_integration import edge
from tests.strategy.graph.test_values import context


def conditions_graph(bars=3, component="close", period=None):
    params = {"component": component}
    if period is not None:
        params["period"] = period
    return {
        "blocks": [
            {"id": "source", "type": "source.value", "params": params},
            {
                "id": "compare",
                "type": "fact.compare",
                "params": {"operator": "gt", "literal": 0.0, "unit": "price_quote_per_base"},
            },
            {"id": "hold", "type": "state.condition_hold", "params": {"bars": bars}},
            {"id": "edge", "type": "fact.condition_edge", "params": {}},
        ],
        "connections": [
            edge("source", "value", "compare", "left"),
            edge("compare", "condition", "hold", "condition"),
            edge("hold", "condition", "edge", "condition"),
        ],
    }


@pytest.mark.parametrize(
    "component,default,unit,history",
    [("adx", 14, "dimensionless", 29), ("relative_volume", 20, "ratio", 21)],
)
def test_new_measurements_have_strict_defaults_units_and_source_history(
    component, default, unit, history
):
    spec = ValueSpecV1(component=component)
    assert (spec.period, spec.unit, spec.required_history_bars()) == (default, unit, history)
    for period in [True, 2.0, 1, 401]:
        with pytest.raises(ValidationError):
            ValueSpecV1(component=component, period=period)
    with pytest.raises(ValidationError):
        ValueSpecV1(component=component, std_dev=2.0)


def test_adx_preserves_wilder_predicates_and_distinguishes_zero_true_range():
    for closes, highs, lows, expected in [
        ([1, 2, 3, 4, 5], [2, 3, 4, 5, 6], [0, 1, 2, 3, 4], 100.0),
        ([5] * 5, [6] * 5, [4] * 5, 0.0),
        ([5] * 5, [5] * 5, [5] * 5, None),
    ]:
        ctx = context(closes, highs=np.array(highs, dtype=float), lows=np.array(lows, dtype=float))
        result = value_at(ctx, ValueSpecV1(component="adx", period=2))
        assert result.value == expected
        assert result.readiness == ("warming_up" if expected is None else "ready")
        if expected is None:
            assert result.reason == "insufficient_valid_dx"
        for threshold in [-1.0, 0.0, 25.0, 100.0, 100.01]:
            assert adx_filter(ctx.highs, ctx.lows, ctx.closes, 2, threshold) == (
                expected is not None and expected >= threshold
            )


def test_adx_requires_enough_valid_dx_and_excludes_offset_suffix():
    ctx = context(
        [5, 5, 5, 5, 6], highs=np.array([5, 5, 5, 5, 7.0]), lows=np.array([5, 5, 5, 5, 5.0])
    )
    result = value_at(ctx, ValueSpecV1(component="adx", period=2))
    assert (result.value, result.reason) == (None, "insufficient_valid_dx")
    base = context([1, 3, 2, 5, 4, 7, 3])
    expected = value_at(base, ValueSpecV1(component="adx", period=2))
    extended = context([1, 3, 2, 5, 4, 7, 3, 10000])
    actual = value_at(extended, ValueSpecV1(component="adx", period=2, offset_bars=1))
    assert actual.value == expected.value
    assert actual.as_of_ms == expected.as_of_ms
    shortened = value_at(context([2, 5, 4, 7, 3]), ValueSpecV1(component="adx", period=2))
    assert shortened.value != expected.value  # Wilder seed uses the retained prefix.


def test_mixed_adx_wilder_threshold_equality():
    # N2 DX samples are 100*3.5/4.5 and 100*.75/3.25; ADX is their mean.
    ctx = context([1, 3, 2, 5, 4])
    result = value_at(ctx, ValueSpecV1(component="adx", period=2))
    assert result.value == 50.42735042735043
    assert adx_filter(ctx.highs, ctx.lows, ctx.closes, 2, 50.42735042735043)
    assert not adx_filter(ctx.highs, ctx.lows, ctx.closes, 2, 50.42735042735044)


@pytest.mark.parametrize(
    "volumes,expected,readiness,reason",
    [
        ([10, 20, 30], 2.0, "ready", None),
        ([10, 20, 0], 0.0, "ready", None),
        ([0, 0, 30], None, "missing", "zero_volume_mean"),
        ([10, float("nan"), 30], None, "invalid", "nonfinite_input"),
        ([10, float("inf"), 30], None, "invalid", "nonfinite_input"),
    ],
)
def test_relative_volume_excludes_current_from_denominator(volumes, expected, readiness, reason):
    result = value_at(
        context(volumes=np.array(volumes)), ValueSpecV1(component="relative_volume", period=2)
    )
    assert (result.value, result.readiness, result.reason) == (expected, readiness, reason)


@pytest.mark.parametrize(
    "change,readiness,reason",
    [
        ({"volumes": None}, "missing", "missing_volumes"),
        ({"volumes": np.ones(2)}, "invalid", "misaligned_history"),
        ({"history_start_ms": 60000}, "invalid", "noncontiguous_history"),
        ({"decision_timestamp_ms": 179999}, "missing", "future_availability"),
        ({"decision_timestamp_ms": 240000}, "stale", "stale_primary_bar"),
    ],
)
def test_relative_volume_never_fabricates_missing_or_bad_inputs(change, readiness, reason):
    result = value_at(context(**change), ValueSpecV1(component="relative_volume", period=2))
    assert (result.value, result.readiness, result.reason) == (None, readiness, reason)


def test_relative_volume_offset_has_exact_physical_requirement_and_causality():
    spec = ValueSpecV1(component="relative_volume", period=2, offset_bars=1)
    assert spec.required_history_bars() == 4
    assert value_at(context(), spec).readiness == "warming_up"
    a = value_at(context([1] * 4, volumes=np.array([10, 20, 30, np.nan])), spec)
    assert a.value == 2.0 and a.as_of_ms == 180000


@pytest.mark.parametrize("component,period,history", [("adx", 3, 8), ("relative_volume", 3, 5)])
def test_new_values_agree_direct_prepared_series_and_recorded(component, period, history):
    params = {"component": component, "period": period, "offset_bars": 1}
    graph = {
        "blocks": [{"id": "source", "type": "source.value", "params": params}],
        "connections": [],
    }
    closes = np.array([1, 3, 2, 5, 4, 7, 3, 9, 6, 10.0])
    rows = np.column_stack(
        [np.arange(10) * 60000, closes, closes + 1, closes - 1, closes, np.arange(10) + 10]
    )
    prepared = PreparedIndicators.build(graph, rows, history)
    series = strategy_indicator_series(
        graph, rows, timeframe="1m", symbol="BTCUSDT", history_bars=history
    )[0]
    executor = GraphExecutor.build(graph)
    for end in range(1, 11):
        start = max(0, end - history)
        ctx = context(
            closes[start:end],
            bar_index=end - 1,
            timestamp_ms=(end - 1) * 60000,
            decision_timestamp_ms=end * 60000,
            history_start_ms=start * 60000,
            volumes=rows[start:end, 5],
            evidence=DecisionRecorder(),
        )
        expected = value_at(ctx, ValueSpecV1(**params))
        actual = executor.step(
            replace(ctx, indicators=prepared.at(ctx.timestamp_ms, len(ctx.closes)))
        ).entities_by_node["source"]["value"]
        assert actual.model_dump(exclude={"source_node_id"}) == expected.model_dump(
            exclude={"source_node_id"}
        )
        assert series["points"][end - 1]["value"] == expected.value
        assert series["formula_version"] == expected.formula_version
        assert ctx.evidence.nodes[0]["outputs"]["value"] == actual.model_dump()


def test_history_reports_physical_rows_separately_from_nested_streaming_latency():
    api = importlib.import_module("koval.strategy.graph.condition_history")
    graph = conditions_graph(400)
    for i in range(2, 5):
        graph["blocks"].insert(
            -1, {"id": f"hold{i}", "type": "state.condition_hold", "params": {"bars": 400}}
        )
        previous = "hold" if i == 2 else f"hold{i - 1}"
        graph["connections"].append(edge(previous, "condition", f"hold{i}", "condition"))
    graph["connections"][2] = edge("hold4", "condition", "edge", "condition")
    info = api.graph_condition_history_requirements(graph)
    assert info["schema"] == "koval_condition_history_v1"
    assert info["source_history_bars"] == 1
    assert info["first_ready_bar_by_node"]["hold4"] == 1
    assert info["first_true_bar_by_node"]["hold4"] == 1597
    assert info["replay_prefix_bars"] == 1598
    GraphExecutor.build(graph)  # Temporal latency is not a retained-array demand.
    for component, expected in [("adx", 801), ("relative_volume", 401)]:
        single = {
            "blocks": [
                {
                    "id": "s",
                    "type": "source.value",
                    "params": {"component": component, "period": 400},
                }
            ]
        }
        assert api.graph_condition_history_requirements(single)["source_history_bars"] == expected


def test_physical_requirements_over_1000_refuse_at_graph_admission():
    graph = {
        "blocks": [
            {
                "id": "s",
                "type": "source.value",
                "params": {"component": "adx", "period": 400, "offset_bars": 200},
            }
        ]
    }
    with pytest.raises(GraphValidationError, match="1000"):
        GraphExecutor.build(graph)


def test_legacy_large_history_stays_supported_and_requirement_is_never_clamped():
    from koval.strategy.graph.values import graph_required_history_bars

    graph = {
        "blocks": [
            {
                "id": "s",
                "type": "source.value",
                "params": {"component": "channel.upper", "period": 400, "offset_bars": 700},
            }
        ]
    }
    assert graph_required_history_bars(graph) == 1101
    GraphExecutor.build(graph)
    report = importlib.import_module(
        "koval.strategy.graph.condition_history"
    ).graph_condition_history_requirements
    assert report(graph)["source_history_bars"] == 1101
    graph["blocks"][0]["params"]["component"] = "relative_volume"
    assert report(graph)["source_history_bars"] == 1101
    with pytest.raises(GraphValidationError, match="1000"):
        GraphExecutor.build(graph)


@pytest.mark.parametrize("changed", ["config", "formula", "cursor", "version"])
def test_strategy_restore_refuses_reducer_mismatch_even_on_consumed_bar(changed):
    first = assemble_from_graph(conditions_graph())
    first.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
    ctx = context()
    for key, value in vars(ctx).items():
        if key not in {"account", "timeframe", "evidence", "indicators", "position_id"}:
            setattr(first, key, value)
    first.on_bar()
    state = deepcopy(first.export_graph_state())
    if changed == "config":
        state["states"]["hold"]["bars"] = 2
    elif changed == "formula":
        state["states"]["edge"]["formula_version"] = "changed"
    elif changed == "cursor":
        state["states"]["hold"]["last_timestamp_ms"] += 60000
    else:
        state["states"]["edge"]["version"] = 2
    before = deepcopy(first.export_graph_state())
    with pytest.raises(ValueError):
        first.restore_graph_state(state)
    assert first.export_graph_state() == before


@pytest.mark.parametrize(
    "change",
    ["advanced", "negative", "boolean", "float", "missing", "empty", "stale_reducer", "initial"],
)
def test_condition_restore_requires_complete_state_at_the_envelope_cursor(change):
    graph = conditions_graph()
    original = assemble_from_graph(graph)
    original.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
    for end in range(1, 3):
        for key, value in vars(context([1] * end)).items():
            if key not in {"account", "timeframe", "evidence", "indicators", "position_id"}:
                setattr(original, key, value)
        original.on_bar()
        if end == 1:
            earlier = original.export_graph_state()
    saved = original.export_graph_state()
    if change == "advanced":
        saved["stepped_bar"] += 1
    elif change == "negative":
        saved["stepped_bar"] = -2
    elif change == "boolean":
        saved["stepped_bar"] = True
    elif change == "float":
        saved["stepped_bar"] = 1.0
    elif change == "missing":
        del saved["states"]["hold"]
    elif change == "empty":
        saved["states"]["hold"] = {}
    elif change == "stale_reducer":
        saved["states"]["edge"] = earlier["states"]["edge"]
    else:
        saved["stepped_bar"] = -1
    target = assemble_from_graph(graph)
    before = target.export_graph_state()
    with pytest.raises(ValueError):
        target.restore_graph_state(saved)
    assert target.export_graph_state() == before


def test_discovery_distinguishes_new_semantics_without_claiming_plugin_execution():
    api = importlib.import_module("koval.strategy.graph.condition_history")
    info = api.condition_capabilities()
    assert info["schema"] == "koval_historical_conditions_capabilities_v1"
    assert info["required_execution_capability"] == "numeric_values_v1"
    assert info["availability_model"] == "historical_ideal_close_v1"
    assert info["modes"] == ["historical"]
    assert info["adapter_execution_verified"] is False
    assert "bars" in info["parameter_schemas"]["state.condition_hold"]["required"]
    assert info["state_versions"] == {"state.condition_hold": 1, "fact.condition_edge": 1}
    strategy = assemble_from_graph(conditions_graph())
    assert strategy.required_execution_capabilities() == {"numeric_values_v1"}


def test_existing_value_spec_dumps_and_formula_identity_remain_unchanged():
    spec = ValueSpecV1(component="bb.width_pct", period=3)
    assert (
        spec.model_dump_json()
        == '{"component":"bb.width_pct","period":3,"std_dev":2.0,"offset_bars":0,"context":"primary"}'
    )
    result = value_at(context(), spec)
    assert result.formula_version == VALUE_FORMULA_VERSION + ":" + spec.model_dump_json()
    assert result.value == 163.2993161855452
    with pytest.raises(ValidationError):
        ValueSpecV1(component="bb_width_pct")


def test_graph_strategy_snapshot_suffix_and_same_bar_suppression():
    graph = conditions_graph()
    first, restored = assemble_from_graph(graph), assemble_from_graph(graph)

    def inject(strategy, values):
        ctx = context(values)
        strategy.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
        for key, value in vars(ctx).items():
            if key not in {"account", "timeframe", "evidence", "indicators", "position_id"}:
                setattr(strategy, key, value)

    for values in [(1,), (1, 1)]:
        inject(first, values)
        first.on_bar()
    saved = json.loads(json.dumps(first.export_graph_state()))
    restored.restore_graph_state(saved)
    inject(restored, (1, 1))
    restored.on_bar()
    assert restored.export_graph_state() == saved
    for values in [(1, 1, 1), (1, 1, 1, 1)]:
        for strategy in (first, restored):
            inject(strategy, values)
            strategy.on_bar()
        assert first._result.entities_by_node == restored._result.entities_by_node
        assert first.export_graph_state() == restored.export_graph_state()
    invalid = deepcopy(saved)
    invalid["states"]["hold"]["count"] = True
    before = deepcopy(restored.export_graph_state())
    with pytest.raises(ValueError):
        restored.restore_graph_state(invalid)
    assert restored.export_graph_state() == before


def test_long_stream_nested_holds_pass_1000_temporal_bars_with_bounded_source_and_restore():
    graph = conditions_graph(400)
    for i in range(2, 5):
        graph["blocks"].insert(
            -1, {"id": f"hold{i}", "type": "state.condition_hold", "params": {"bars": 400}}
        )
        graph["connections"].append(
            edge("hold" if i == 2 else f"hold{i - 1}", "condition", f"hold{i}", "condition")
        )
    graph["connections"][2] = edge("hold4", "condition", "edge", "condition")
    original, restored = assemble_from_graph(graph), assemble_from_graph(graph)
    for strategy in (original, restored):
        strategy.config = {"symbol": "BTCUSDT", "timeframe": "1m"}

    def inject(strategy, index):
        start = max(0, index - 999)
        ctx = context(
            [1] * (index - start + 1),
            bar_index=index,
            timestamp_ms=index * 60000,
            decision_timestamp_ms=(index + 1) * 60000,
            history_start_ms=start * 60000,
        )
        for key, value in vars(ctx).items():
            if key not in {"account", "timeframe", "evidence", "indicators", "position_id"}:
                setattr(strategy, key, value)

    event_bars = []
    snapshot_size = None
    for i in range(1600):
        inject(original, i)
        original.on_bar()
        if original._result.entities_by_node["edge"]["event"] is not None:
            event_bars.append(i + 1)
        if i == 1000:
            state = json.loads(json.dumps(original.export_graph_state()))
            snapshot_size = len(json.dumps(state))
            restored.restore_graph_state(state)
            inject(restored, i)
            restored.on_bar()
            assert restored.export_graph_state() == state
            assert restored._result.entities_by_node == {}
        elif i > 1000:
            inject(restored, i)
            restored.on_bar()
            assert original._result.entities_by_node == restored._result.entities_by_node
            assert original.export_graph_state() == restored.export_graph_state()
    assert event_bars == [1597]
    assert original._result.entities_by_node["hold4"]["condition"].value is True
    assert len(original.closes) == 1000
    assert len(json.dumps(original.export_graph_state())) < snapshot_size + 1000
