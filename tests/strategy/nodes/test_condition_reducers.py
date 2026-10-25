"""Strict, bounded hold and rising-edge state machine contract."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from koval.strategy.block_assembler import GraphValidationError
from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import ConditionV1, MarketEvent
from koval.strategy.graph.executor import GraphExecutor
from koval.strategy.graph.registry import get_node
from tests.strategy.graph.test_historical_conditions import conditions_graph
from tests.strategy.graph.test_value_integration import edge
from tests.strategy.graph.test_values import context


def condition(value, ctx, **changes):
    return ConditionV1(
        **(
            dict(
                value=value,
                unit="ratio",
                symbol=ctx.symbol,
                timeframe=ctx.timeframe,
                as_of_ms=ctx.decision_timestamp_ms,
                available_at_ms=ctx.decision_timestamp_ms,
                readiness="ready" if value is not None else "missing",
                reason=None if value is not None else "missing_volume",
                formula_version="fixture_v1",
                input_cursor=str(ctx.timestamp_ms),
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="fixture",
            )
            | changes
        )
    )


def step(kind, state, index, value, bars=3, changes=None, ctx_changes=None):
    ctx = context(
        (1,),
        bar_index=index,
        timestamp_ms=index * 60000,
        history_start_ms=index * 60000,
        decision_timestamp_ms=(index + 1) * 60000,
        **(ctx_changes or {}),
    )
    spec = get_node(kind)
    params = spec.params_schema(**({"bars": bars} if kind == "state.condition_hold" else {}))
    return spec.factory(params)(
        ctx, {"condition": [condition(value, ctx, **(changes or {}))]}, state
    )


def test_hold_and_edge_are_strict_condition_nodes_with_correct_domains():
    for kind, domain, output, entity in [
        ("state.condition_hold", Domain.STATE, "condition", ConditionV1),
        ("fact.condition_edge", Domain.FACT, "event", MarketEvent),
    ]:
        spec = get_node(kind)
        assert spec.domain == domain
        assert spec.input_ports["condition"].accepts == (ConditionV1,)
        assert spec.input_ports["condition"].max_connections == 1
        assert spec.output_ports[output].accepts == (entity,)
    for params in [{}, {"bars": True}, {"bars": 1.0}, {"bars": 0}, {"bars": -1}, {"bars": 401}]:
        with pytest.raises(ValidationError):
            get_node("state.condition_hold").params_schema(**params)
    for bars in [1, 400]:
        get_node("state.condition_hold").params_schema(bars=bars)


@pytest.mark.parametrize("kind", ["state.condition_hold", "fact.condition_edge"])
def test_reducers_refuse_policy_input_and_fan_in(kind):
    graph = {
        "blocks": [
            {"id": "p", "type": "policy.ema_trend", "params": {}},
            {"id": "r", "type": kind, "params": {"bars": 3} if kind.startswith("state") else {}},
        ],
        "connections": [edge("p", "policy", "r", "condition")],
    }
    with pytest.raises(GraphValidationError, match="domain|type"):
        GraphExecutor.build(graph)
    graph = conditions_graph()
    graph["connections"].append(edge("compare", "condition", "edge", "condition"))
    with pytest.raises(GraphValidationError, match="at most 1"):
        GraphExecutor.build(graph)


@pytest.mark.parametrize(
    "values,expected",
    [
        ([True, True, True, True], [False, False, True, True]),
        ([True, None, True, True, True], [False, None, False, False, True]),
        ([True, False, True, True, True], [False, False, False, False, True]),
    ],
)
def test_hold_counts_consecutive_true_and_propagates_unavailable(values, expected):
    state = {}
    actual = [
        step("state.condition_hold", state, i, value)["condition"] for i, value in enumerate(values)
    ]
    assert [v.value for v in actual] == expected
    assert state["count"] == 3
    if None in values:
        unavailable = actual[values.index(None)]
        assert (unavailable.readiness, unavailable.reason) == ("missing", "missing_volume")


@pytest.mark.parametrize("kind", ["state.condition_hold", "fact.condition_edge"])
def test_duplicate_conflicts_do_not_revise_state_and_backwards_refuses_before_mutation(kind):
    state = {}
    first = step(kind, state, 2, False)
    saved = deepcopy(state)
    repeated = step(kind, state, 2, True)
    assert repeated == first
    assert state == saved
    with pytest.raises(ValueError, match="backwards"):
        step(kind, state, 1, True)
    assert state == saved
    with pytest.raises(ValueError, match="duplicate.*binding"):
        step(kind, state, 2, True, changes={"formula_version": "changed"})
    assert state == saved


def test_hold_gap_binding_and_sliding_history_reset_only_real_discontinuities():
    state = {}
    for i in range(3):
        assert step("state.condition_hold", state, i, True)["condition"].value is (i == 2)
    assert step("state.condition_hold", state, 4, True)["condition"].value is False
    assert state["count"] == 1
    assert (
        step("state.condition_hold", state, 5, True, changes={"input_cursor": "moving-history"})[
            "condition"
        ].value
        is False
    )
    assert state["count"] == 2
    assert step("state.condition_hold", state, 6, True)["condition"].value is True
    assert (
        step("state.condition_hold", state, 7, True, changes={"formula_version": "new"})[
            "condition"
        ].value
        is False
    )
    assert state["count"] == 1
    assert (
        step("state.condition_hold", state, 8, True, ctx_changes={"symbol": "ETHUSDT"})[
            "condition"
        ].value
        is False
    )
    assert state["count"] == 1


@pytest.mark.parametrize("kind", ["state.condition_hold", "fact.condition_edge"])
@pytest.mark.parametrize("changes", [{"timestamp_ms": 0}, {"available_at_ms": 999999}])
def test_bad_input_clocks_disarm_without_coercion(kind, changes):
    state = {}
    step(kind, state, 1, False)
    output = step(kind, state, 2, True, changes=changes)
    if kind.startswith("state"):
        assert output["condition"].value is None
        assert state["count"] == 0
    else:
        assert output["event"] is None
        assert step(kind, state, 3, True)["event"] is None


@pytest.mark.parametrize(
    "values,indices,expected",
    [
        ([False, True, True], [0, 1, 2], [False, True, False]),
        ([True, True], [0, 1], [False, False]),
        ([None, True], [0, 1], [False, False]),
        ([False, None, True], [0, 1, 2], [False, False, False]),
        ([False, True], [0, 2], [False, False]),
    ],
)
def test_edge_requires_consecutive_ready_false_then_true(values, indices, expected):
    state = {}
    outputs = [
        step("fact.condition_edge", state, i, value)["event"]
        for i, value in zip(indices, values, strict=True)
    ]
    assert [v is not None for v in outputs] == expected
    for event in filter(None, outputs):
        assert (event.kind, event.direction, event.price_levels) == (
            "condition_edge",
            "bullish",
            [],
        )
        assert event.metadata["previous"]["value"] is False
        assert event.metadata["current"]["value"] is True
        event.metadata["current"]["value"] = False
        assert state["previous"]["value"] is True


def test_hold_three_edge_fires_third_bar_and_hold_one_does_not_invent_prior_false():
    for bars, expected in [(3, [False, False, True, False]), (1, [False, False, False, False])]:
        executor = GraphExecutor.build(conditions_graph(bars))
        outputs = [executor.step(context([1] * i)).entities_by_node for i in range(1, 5)]
        assert [v["edge"]["event"] is not None for v in outputs] == expected
        if bars == 3:
            assert outputs[2]["edge"]["event"].source_node_id == "edge"


@pytest.mark.parametrize("kind", ["state.condition_hold", "fact.condition_edge"])
def test_snapshot_suffix_duplicate_and_bounded_state(kind):
    original = {}
    for i, value in enumerate([False, True, True]):
        step(kind, original, i, value)
    restored = json.loads(json.dumps(original))
    for i, value in [(2, False), (3, True), (4, False), (5, True)]:
        assert step(kind, original, i, value) == step(kind, restored, i, value)
        assert original == restored
    for i in range(6, 1500):
        step(kind, original, i, True)
    assert len(json.dumps(original)) < 2500


@pytest.mark.parametrize("kind", ["state.condition_hold", "fact.condition_edge"])
@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"version": True},
        {"last_timestamp_ms": -1},
        {"last_bar_index": 1.0},
        {"formula_version": "wrong"},
        {"unexpected": 1},
    ],
)
def test_malformed_or_unsupported_state_refuses_before_mutation(kind, change):
    state = {}
    step(kind, state, 0, True)
    state.update(change)
    saved = deepcopy(state)
    with pytest.raises(ValueError):
        step(kind, state, 1, True)
    assert state == saved


@pytest.mark.parametrize("change", [{"count": True}, {"count": -1}, {"count": 4}, {"bars": 2}])
def test_hold_rejects_invalid_count_or_changed_own_config(change):
    state = {}
    step("state.condition_hold", state, 0, True)
    state.update(change)
    saved = deepcopy(state)
    with pytest.raises(ValueError):
        step("state.condition_hold", state, 1, True)
    assert state == saved


@pytest.mark.parametrize("kind", ["state.condition_hold", "fact.condition_edge"])
def test_restored_changed_input_binding_resets_on_next_cursor(kind):
    state = {}
    step(kind, state, 0, False)
    restored = json.loads(json.dumps(state))
    output = step(kind, restored, 1, True, changes={"formula_version": "new_formula"})
    assert output.get("event") is None
    if kind.startswith("state"):
        assert restored["count"] == 1


def test_hold_maximum_saturates_without_retaining_a_window():
    state = {}
    for i in range(401):
        output = step("state.condition_hold", state, i, True, bars=400)["condition"]
        assert output.value is (i >= 399)
    assert state["count"] == 400
    assert len(json.dumps(state)) < 2500


@pytest.mark.parametrize("kind", ["state.condition_hold", "fact.condition_edge"])
@pytest.mark.parametrize("timeframe", [None, "unsupported"])
def test_missing_context_after_unavailable_does_not_crash_or_arm(kind, timeframe):
    state = {}
    for i in range(2):
        output = step(kind, state, i, None, ctx_changes={"timeframe": timeframe})
        if kind.startswith("state"):
            assert output["condition"].value is None
        else:
            assert output["event"] is None


@pytest.mark.parametrize("kind", ["state.condition_hold", "fact.condition_edge"])
def test_advancing_timestamp_with_backwards_primary_index_refuses(kind):
    state = {}
    step(kind, state, 3, True)
    saved = deepcopy(state)
    ctx = context(
        (1,),
        bar_index=2,
        timestamp_ms=240000,
        history_start_ms=240000,
        decision_timestamp_ms=300000,
    )
    spec = get_node(kind)
    params = spec.params_schema(**({"bars": 3} if kind.startswith("state") else {}))
    with pytest.raises(ValueError, match="backwards"):
        spec.factory(params)(ctx, {"condition": [condition(True, ctx)]}, state)
    assert state == saved
