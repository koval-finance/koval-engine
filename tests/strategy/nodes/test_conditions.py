"""Typed comparison, crossing, distance and policy conversion contracts."""

import json

import pytest
from pydantic import ValidationError

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.registry import get_all_nodes, get_node
from tests.strategy.graph.test_values import context

NODES = {"source.value", "fact.compare", "fact.cross", "fact.distance", "policy.from_condition"}


@pytest.fixture
def api():
    assert NODES <= {s.type for s in get_all_nodes()}, "numeric/condition nodes are missing"
    from koval.strategy.graph.entities import NumericValueV1

    return NumericValueV1


def number(api, val, *, ctx=None, **changes):
    ctx = ctx or context()
    data = dict(
        value=float(val),
        unit="price_quote_per_base",
        symbol=ctx.symbol,
        timeframe=ctx.timeframe,
        as_of_ms=ctx.decision_timestamp_ms,
        available_at_ms=ctx.decision_timestamp_ms,
        readiness="ready",
        reason=None,
        formula_version="fixture_v1",
        input_cursor=str(ctx.timestamp_ms),
        bar_index=ctx.bar_index,
        timestamp_ms=ctx.timestamp_ms,
        source_node_id="fixture",
    )
    return api(**(data | changes))


def evaluate(kind, inputs, params=None, state=None, ctx=None):
    spec = get_node(kind)
    return spec.factory(spec.params_schema(**(params or {})))(
        ctx or context(), inputs, {} if state is None else state
    )


def test_public_nodes_have_scalar_ports_and_source_domain(api):
    source = get_node("source.value")
    assert source.domain == Domain.SOURCE and source.input_ports == {}
    assert source.output_ports["value"].accepts == (api,)
    for kind in NODES - {"source.value"}:
        assert all(p.max_connections == 1 for p in get_node(kind).input_ports.values())


@pytest.mark.parametrize(
    "operator,left,right,expected",
    [
        ("gt", 3, 2, True),
        ("gt", 2, 2, False),
        ("gte", 2, 2, True),
        ("lt", 1, 2, True),
        ("lte", 2, 2, True),
        ("eq", 2, 2, True),
        ("ne", 2, 2, False),
    ],
)
def test_comparison_operators_exact_equality(api, operator, left, right, expected):
    inputs = {"left": [number(api, left)], "right": [number(api, right)]}
    result = evaluate("fact.compare", inputs, {"operator": operator})["condition"]
    assert result.value is expected and result.readiness == "ready"
    literal = evaluate(
        "fact.compare",
        {"left": inputs["left"]},
        {"operator": operator, "literal": float(right), "unit": "price_quote_per_base"},
    )["condition"]
    assert literal.value == result.value


def test_between_is_inclusive_and_exclusive_is_explicit(api):
    for inclusive, expected in [(True, True), (False, False)]:
        result = evaluate(
            "fact.compare",
            {"left": [number(api, 2)]},
            {
                "operator": "between",
                "lower": 2.0,
                "upper": 3.0,
                "unit": "price_quote_per_base",
                "inclusive": inclusive,
            },
        )["condition"]
        assert result.value is expected


@pytest.mark.parametrize(
    "changes", [{"unit": "percent"}, {"symbol": "ETHUSDT"}, {"timeframe": "5m"}]
)
def test_identity_mismatch_rejected(api, changes):
    with pytest.raises(ValueError, match="identity"):
        evaluate(
            "fact.compare",
            {"left": [number(api, 1)], "right": [number(api, 2, **changes)]},
            {"operator": "gt"},
        )


def test_unavailable_compare_and_bridge_preserve_reason(api):
    left = number(api, 1, value=None, readiness="warming_up", reason="insufficient_history")
    condition = evaluate(
        "fact.compare", {"left": [left], "right": [number(api, 2)]}, {"operator": "gt"}
    )["condition"]
    assert (condition.value, condition.readiness, condition.reason) == (
        None,
        "warming_up",
        "insufficient_history",
    )
    policy = evaluate("policy.from_condition", {"condition": [condition]})["policy"]
    assert not policy.allowed and policy.metadata["readiness"] == "warming_up"
    assert policy.metadata["reason"] == "insufficient_history"


@pytest.mark.parametrize(
    "params",
    [
        {"operator": "eq", "literal": float("nan"), "unit": "percent"},
        {"operator": "between", "lower": 3.0, "upper": 2.0, "unit": "percent"},
        {"operator": "between", "lower": 1.0},
        {"operator": "gt", "lower": 1.0},
        {"operator": "gt", "literal": 1.0},
        {"operator": "eq", "epsilon": 0.1},
    ],
)
def test_comparison_invalid_parameters(api, params):
    with pytest.raises(ValidationError):
        get_node("fact.compare").params_schema(**params)


def test_cross_strict_transition_duplicate_cursor_gap_and_json_restore(api):
    state = {}

    def step(left, right, index, *, target=state, missing=False):
        ctx = context(
            (1, 2, 3),
            bar_index=index,
            timestamp_ms=index * 60_000,
            decision_timestamp_ms=(index + 1) * 60_000,
        )
        a = number(api, left, ctx=ctx)
        if missing:
            a = number(api, left, ctx=ctx, value=None, readiness="missing", reason="missing_input")
        return evaluate(
            "fact.cross",
            {"left": [a], "right": [number(api, right, ctx=ctx)]},
            {"direction": "both"},
            target,
            ctx,
        )["event"]

    assert step(1, 1, 2) is None
    saved = json.loads(json.dumps(state))
    event = step(2, 1, 3)
    assert event.direction == "bullish" and event.metadata["direction"] == "up"
    assert event.metadata["right"]["value"] == 1
    assert step(2, 1, 3) is None
    assert step(2, 1, 3, target=saved) == event
    assert step(1, 1, 4) is None
    assert step(1, 1, 5) is None
    assert step(0, 1, 6).direction == "bearish"
    assert step(2, 1, 8) is None  # Missing candle must re-arm.
    assert step(0, 1, 9, missing=True) is None
    assert step(2, 1, 10) is None
    assert step(0, 1, 11).direction == "bearish"


@pytest.mark.parametrize(
    "mode,price,expected,unit",
    [
        ("percent", 110, 10, "percent"),
        ("atr", 110, 2, "ratio"),
        ("percent", 90, -10, "percent"),
        ("atr", 90, -2, "ratio"),
    ],
)
def test_signed_distance(api, mode, price, expected, unit):
    inputs = {"price": [number(api, price)], "level": [number(api, 100)]}
    if mode == "atr":
        inputs["atr"] = [number(api, 5)]
    result = evaluate("fact.distance", inputs, {"mode": mode})["value"]
    assert (result.value, result.unit, result.readiness) == (expected, unit, "ready")


def test_distance_denominator_and_missing_never_signal(api):
    for level in (0, -1):
        result = evaluate(
            "fact.distance",
            {"price": [number(api, 100)], "level": [number(api, level)]},
            {"mode": "percent"},
        )["value"]
        assert result.value is None and result.readiness == "invalid"
    result = evaluate(
        "fact.distance", {"price": [number(api, 100)], "level": [number(api, 90)]}, {"mode": "atr"}
    )["value"]
    assert result.value is None and result.readiness == "missing"


def test_cross_future_availability_and_stale_input_do_not_arm(api):
    state = {}
    for changes in ({"available_at_ms": 180_001}, {"timestamp_ms": 60_000}):
        out = evaluate(
            "fact.cross",
            {"left": [number(api, 1, **changes)], "right": [number(api, 1)]},
            {},
            state,
        )
        assert out["event"] is None
        assert state.get("previous") is None


def test_condition_false_is_ready_and_bridge_false(api):
    condition = evaluate(
        "fact.compare", {"left": [number(api, 1)], "right": [number(api, 2)]}, {"operator": "gt"}
    )["condition"]
    policy = evaluate("policy.from_condition", {"condition": [condition]})["policy"]
    assert condition.value is False and condition.readiness == "ready"
    assert policy.allowed is False and policy.metadata["readiness"] == "ready"


def test_node_contract_is_registered():
    assert NODES <= {s.type for s in get_all_nodes()}, "all five primitives must be registered"


@pytest.mark.parametrize(
    "kind,ports,params",
    [
        ("fact.distance", ("price", "level"), {"mode": "percent"}),
        ("fact.cross", ("left", "right"), {}),
    ],
)
@pytest.mark.parametrize(
    "changes", [{"unit": "percent"}, {"symbol": "ETHUSDT"}, {"timeframe": "5m"}]
)
def test_distance_cross_identity_mismatch_rejected(api, kind, ports, params, changes):
    with pytest.raises(ValueError, match="identity"):
        evaluate(kind, {ports[0]: [number(api, 10)], ports[1]: [number(api, 5, **changes)]}, params)


def test_connected_unavailable_or_absent_right_is_null(api):
    result = evaluate("fact.compare", {"left": [number(api, 2)]}, {"operator": "eq"})["condition"]
    assert (result.value, result.readiness) == (None, "missing")


def test_scalar_model_json_roundtrip_and_strict_boolean(api):
    condition = evaluate(
        "fact.compare", {"left": [number(api, 2)], "right": [number(api, 2)]}, {"operator": "eq"}
    )["condition"]
    assert type(condition).model_validate_json(condition.model_dump_json()) == condition
    with pytest.raises(ValidationError):
        type(condition).model_validate(condition.model_dump() | {"value": 1})
    with pytest.raises(ValidationError):
        api.model_validate(number(api, 2).model_dump() | {"value": True})


def test_condition_formula_identity_includes_operator_and_literal(api):
    inputs = {"left": [number(api, 2)]}
    a = evaluate(
        "fact.compare", inputs, {"operator": "gt", "literal": 1.0, "unit": "price_quote_per_base"}
    )["condition"]
    b = evaluate(
        "fact.compare", inputs, {"operator": "gte", "literal": 1.0, "unit": "price_quote_per_base"}
    )["condition"]
    assert a.formula_version != b.formula_version


def test_cross_direction_filter_still_advances_state_and_rejects_unknown_version(api):
    state = {}
    for index, left, expected in [(2, 1, None), (3, 2, None), (4, 0, "bearish")]:
        ctx = context(
            bar_index=index, timestamp_ms=index * 60000, decision_timestamp_ms=(index + 1) * 60000
        )
        event = evaluate(
            "fact.cross",
            {"left": [number(api, left, ctx=ctx)], "right": [number(api, 1, ctx=ctx)]},
            {"direction": "down"},
            state,
            ctx,
        )["event"]
        assert (None if event is None else event.direction) == expected
    with pytest.raises(ValueError, match="version"):
        evaluate(
            "fact.cross", {"left": [number(api, 1)], "right": [number(api, 1)]}, {}, {"version": 99}
        )


def test_cross_emitted_metadata_cannot_mutate_future_persistent_state(api):
    state = {}

    def step(left, index):
        ctx = context(
            bar_index=index, timestamp_ms=index * 60000, decision_timestamp_ms=(index + 1) * 60000
        )
        return evaluate(
            "fact.cross",
            {"left": [number(api, left, ctx=ctx)], "right": [number(api, 1, ctx=ctx)]},
            {},
            state,
            ctx,
        )["event"]

    assert step(1, 2) is None
    event = step(2, 3)
    saved = json.loads(json.dumps(state))
    event.metadata["left"]["value"] = 0.0
    assert state == saved
    assert step(3, 4) is None
