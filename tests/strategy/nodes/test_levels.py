"""Frozen level semantics: literal portable oracles, state integrity and replay."""

import json
from copy import deepcopy
from importlib.resources import files

import numpy as np
import pytest
from pydantic import ValidationError

from koval.strategy.graph import entities
from koval.strategy.graph.entities import MarketEvent, NumericValueV1
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.registry import get_node

PARAMS = {"mode": "sweep_reclaim", "expires_after_bars": 3, "invalidation_buffer_pct": 0.0}


def context(index, *, open=105.0, high=112.0, low=101.0, close=105.0, **changes):
    return BarContext(
        **(
            dict(
                open=open,
                high=high,
                low=low,
                close=close,
                volume=100.0,
                bar_index=index,
                timestamp_ms=index * 60000,
                decision_timestamp_ms=(index + 1) * 60000,
                symbol="BTCUSDT",
                timeframe="1m",
                history_start_ms=index * 60000,
                opens=np.array([open]),
                highs=np.array([high]),
                lows=np.array([low]),
                closes=np.array([close]),
                volumes=np.array([100.0]),
            )
            | changes
        )
    )


def numeric(ctx, value=100.0, **changes):
    return NumericValueV1(
        **(
            dict(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="bound",
                value=value,
                unit="price_quote_per_base",
                symbol=ctx.symbol,
                timeframe=ctx.timeframe,
                as_of_ms=ctx.timestamp_ms + 60000,
                available_at_ms=ctx.timestamp_ms + 60000,
                readiness="ready",
                reason=None,
                formula_version="literal_bound_v1",
                input_cursor=str(ctx.timestamp_ms),
            )
            | changes
        )
    )


def trigger(ctx, **changes):
    return MarketEvent(
        **(
            dict(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="trigger",
                kind="condition_edge",
                direction="bullish",
            )
            | changes
        )
    )


def evaluate(
    state,
    index,
    *,
    params=None,
    event=False,
    lower=100.0,
    upper=110.0,
    ctx_changes=None,
    lower_changes=None,
    event_changes=None,
):
    ctx = context(index, **(ctx_changes or {}))
    inputs = {
        "trigger": [trigger(ctx, **(event_changes or {}))] if event else [],
        "lower": [] if lower is None else [numeric(ctx, lower, **(lower_changes or {}))],
        "upper": [] if upper is None else [numeric(ctx, upper)],
    }
    spec = get_node("state.level_setup")
    return spec.factory(spec.params_schema(**(params or PARAMS)))(ctx, inputs, state)


def test_contract_is_present_without_changing_legacy_event_defaults():
    assert hasattr(entities, "LevelSnapshotV1"), "missing LevelSnapshotV1 public contract"
    spec = get_node("state.level_setup")
    assert spec.domain.value == "state"
    assert list(spec.input_ports) == ["trigger", "lower", "upper"]
    assert all(p.max_connections == 1 for p in spec.input_ports.values())
    assert spec.input_ports["upper"].required is False
    assert spec.input_ports["trigger"].accepts == (MarketEvent,)
    assert spec.output_ports["level"].accepts == (entities.LevelSnapshotV1,)
    assert spec.output_ports["event"].terminal is False
    assert "event_id" not in MarketEvent.model_fields
    assert "readiness" not in MarketEvent.model_fields


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"mode": "breakout"},
        PARAMS | {"expires_after_bars": True},
        PARAMS | {"expires_after_bars": 1.0},
        PARAMS | {"expires_after_bars": 0},
        PARAMS | {"expires_after_bars": 401},
        PARAMS | {"invalidation_buffer_pct": True},
        PARAMS | {"invalidation_buffer_pct": float("nan")},
        PARAMS | {"invalidation_buffer_pct": float("inf")},
        PARAMS | {"invalidation_buffer_pct": -0.1},
        PARAMS | {"invalidation_buffer_pct": 100.0},
        PARAMS | {"tolerance_pct": 0.0},
        PARAMS | {"mode": "breakout_retest"},
        PARAMS | {"mode": "breakout_retest", "tolerance_pct": True},
        PARAMS | {"mode": "breakout_retest", "tolerance_pct": 100.0},
        PARAMS | {"mode": "short"},
        PARAMS | {"extra": 1},
        PARAMS | {"rearm": "always"},
    ],
)
def test_strict_required_parameters(params):
    with pytest.raises(ValidationError):
        get_node("state.level_setup").params_schema(**params)


def test_portable_literal_lifecycle_traces():
    fixture = json.loads(
        files("koval").joinpath("examples/runtime/historical_levels_v1.json").read_text()
    )
    assert fixture["schema"] == "koval_historical_levels_fixture_v1"
    for case in fixture["cases"]:
        state, level_id = {}, None
        for row in case["bars"]:
            result = evaluate(
                state,
                row["index"],
                params=case["params"],
                event=row.get("trigger", False),
                lower=row.get("lower", 100.0),
                upper=row.get("upper", 110.0),
                ctx_changes=row["ohlc"],
            )
            level = result["level"]
            assert level.status == row["expected_status"], case["name"]
            assert (result["event"] is not None) == row["expected_event"], case["name"]
            if row.get("same_id"):
                assert level.level_id == level_id
            if level.level_id:
                level_id = level.level_id
                assert (level.lower, level.upper, level.center) == tuple(case["frozen"])
            else:
                assert (level.lower, level.upper, level.center) == (None, None, None)
            saved = deepcopy(state)
            repeated = evaluate(
                state,
                row["index"],
                params=case["params"],
                event=row.get("trigger", False),
                lower=row.get("lower", 100.0),
                upper=row.get("upper", 110.0),
                ctx_changes=row["ohlc"],
            )
            assert repeated["event"] is None
            assert repeated["level"] == level
            assert state == saved


@pytest.mark.parametrize(
    "bound,changes,raises",
    [
        (None, {}, False),
        (0.0, {}, False),
        (-1.0, {}, False),
        (120.0, {}, False),
        (100.0, {"symbol": "ETHUSDT"}, True),
        (100.0, {"unit": "ratio"}, True),
        (100.0, {"available_at_ms": 900000}, False),
        (100.0, {"timestamp_ms": 0}, False),
        (None, {"readiness": "missing", "reason": "missing_bound"}, False),
    ],
)
def test_unavailable_and_invalid_bounds_never_fabricate_a_level(bound, changes, raises):
    state = {}
    if raises:
        with pytest.raises(ValueError):
            evaluate(state, 1, event=True, lower=bound, lower_changes=changes)
        assert state == {}
    else:
        result = evaluate(state, 1, event=True, lower=bound, lower_changes=changes)
        assert result["level"].status == "unavailable"
        assert result["level"].readiness != "ready"
        assert result["level"].reason
        assert result["level"].level_id is None


def test_offsets_are_allowed_and_live_bounds_can_disappear_after_arming():
    state = {}
    first = evaluate(
        state, 1, event=True, lower_changes={"as_of_ms": 60000, "available_at_ms": 60000}
    )
    result = evaluate(
        state, 2, lower=None, upper=None, ctx_changes={"open": 100.0, "low": 99.0, "close": 101.0}
    )
    assert result["event"] is not None
    assert result["level"].level_id == first["level"].level_id
    assert result["level"].lower == 100.0


@pytest.mark.parametrize(
    "change",
    [
        {"ctx_changes": {"close": 106.0}},
        {"ctx_changes": {"open": 106.0}},
        {"event": True},
        {"lower": 101.0},
        {"upper": 111.0},
        {"ctx_changes": {"decision_timestamp_ms": 120001}},
    ],
)
def test_conflicting_duplicate_refuses_without_mutation(change):
    state = {}
    evaluate(state, 1, event=True)
    evaluate(state, 2)
    saved = deepcopy(state)
    with pytest.raises(ValueError, match="duplicate"):
        evaluate(state, 2, **change)
    assert state == saved


def test_backward_clock_and_context_identity_refuse_without_mutation():
    state = {}
    evaluate(state, 2, event=True)
    saved = deepcopy(state)
    for index, changes in [
        (1, {}),
        (3, {"symbol": "ETHUSDT"}),
        (3, {"bar_index": 1}),
        (3, {"decision_timestamp_ms": 1}),
    ]:
        with pytest.raises(ValueError):
            evaluate(state, index, ctx_changes=changes)
        assert state == saved


def test_new_trigger_only_rearms_after_terminal_cursor_and_stale_replay_cannot_arm():
    state = {}
    first = evaluate(state, 0, event=True)["level"]
    consumed = evaluate(
        state, 1, event=True, ctx_changes={"open": 100.0, "low": 99.0, "close": 101.0}
    )
    assert consumed["level"].status == "consumed"
    later = evaluate(state, 2, event=True)["level"]
    assert later.status == "armed" and later.level_id != first.level_id
    assert evaluate(state, 3, event=True)["level"].level_id == later.level_id
    evaluate(state, 4, ctx_changes={"open": 99.0, "low": 98.0, "close": 101.0})
    replay = evaluate(state, 5, event=True, event_changes={"timestamp_ms": 0, "bar_index": 0})
    assert replay["level"].status == "invalidated"
    assert replay["event"] is None


@pytest.mark.parametrize(
    "params,changes",
    [
        (PARAMS, {"version": 2}),
        (PARAMS, {"version": True}),
        (PARAMS, {"formula_version": "wrong"}),
        (PARAMS, {"config_hash": "wrong"}),
        (PARAMS | {"expires_after_bars": 4}, {}),
        (PARAMS, {"last_bar_index": -1}),
        (PARAMS, {"extra": True}),
    ],
)
def test_malformed_or_reinterpreted_checkpoint_refuses_before_mutation(params, changes):
    state = {}
    evaluate(state, 0, event=True)
    state.update(changes)
    saved = deepcopy(state)
    with pytest.raises(ValueError):
        evaluate(state, 1, params=params)
    assert state == saved


@pytest.mark.parametrize("status", ["unavailable", "armed", "consumed", "expired", "invalidated"])
def test_every_transition_roundtrips_then_duplicate_and_suffix_match(status):
    original = {}
    evaluate(original, 0, event=status != "unavailable")
    index = 0
    if status == "consumed":
        index = 1
        evaluate(original, index, ctx_changes={"open": 100.0, "low": 99.0, "close": 101.0})
    elif status == "expired":
        for index in range(1, 5):
            evaluate(original, index)
    elif status == "invalidated":
        index = 1
        evaluate(original, index, ctx_changes={"open": 99.0, "low": 98.0, "close": 101.0})
    assert original["output"]["status"] == status
    restored = json.loads(json.dumps(original))
    for j in range(index + 1, index + 6):
        assert evaluate(original, j, event=j == index + 2) == evaluate(
            restored, j, event=j == index + 2
        )
        assert original == restored


def test_terminal_snapshot_tampering_and_node_identity_refuse():
    from koval.strategy.nodes.levels import validate_level_setup_state

    spec = get_node("state.level_setup")
    params = spec.params_schema(**PARAMS)
    state = {}
    evaluate(state, 0, event=True)
    evaluate(state, 1, ctx_changes={"open": 100.0, "low": 99.0, "close": 101.0})
    for key, value in [
        ("lower", 101.0),
        ("level_id", "forged"),
        ("status", "armed"),
        ("event_id", None),
        ("center", 999.0),
        ("expires_bar", 99),
    ]:
        changed = deepcopy(state)
        changed["output"][key] = value
        with pytest.raises(ValueError):
            validate_level_setup_state(params, changed, node_id="state.level_setup")
    with pytest.raises(ValueError):
        validate_level_setup_state(params, state, node_id="another-node")


def test_consumed_snapshot_binding_oracle_is_same_event_and_decision_only():
    from koval.strategy.graph.levels import level_event_binding

    state = {}
    evaluate(state, 0, event=True)
    ctx = context(1, open=100.0, low=99.0, close=101.0)
    result = evaluate(state, 1, ctx_changes={"open": 100.0, "low": 99.0, "close": 101.0})
    assert level_event_binding(ctx, result["level"], result["event"])
    assert not level_event_binding(context(2), result["level"], result["event"])
    wrong = result["event"].model_copy(deep=True)
    wrong.metadata["level_id"] = "wrong"
    assert not level_event_binding(ctx, result["level"], wrong)


@pytest.mark.parametrize(
    "changes", [{"lower": None}, {"lower": 0.0}, {"ctx_changes": {"decision_timestamp_ms": None}}]
)
def test_invalid_rearm_attempt_preserves_latest_terminal_snapshot(changes):
    state = {}
    evaluate(state, 0, event=True)
    before = evaluate(state, 1, ctx_changes={"open": 100.0, "low": 99.0, "close": 101.0})["level"]
    result = evaluate(state, 2, event=True, **changes)
    assert result["level"].status == "consumed"
    assert result["level"].level_id == before.level_id
    assert result["event"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"decision_timestamp_ms": None},
        {"closes": None},
        {"low": float("nan")},
        {"decision_timestamp_ms": 180000},
    ],
)
def test_missing_or_invalid_primary_bar_invalidates_before_entry(changes):
    state = {}
    evaluate(state, 0, event=True)
    result = evaluate(state, 1, ctx_changes={"open": 100.0, "low": 99.0, "close": 101.0} | changes)
    assert result["level"].status == "invalidated"
    assert result["event"] is None


def test_direct_input_wrong_type_fanin_and_nonjson_trigger_refuse_before_mutation():
    spec = get_node("state.level_setup")
    run = spec.factory(spec.params_schema(**PARAMS))
    c = context(0)
    for inputs in [
        {"trigger": [numeric(c)], "lower": [numeric(c)]},
        {"trigger": [trigger(c), trigger(c)], "lower": [numeric(c)]},
        {"trigger": [trigger(c, metadata={"bad": float("nan")})], "lower": [numeric(c)]},
    ]:
        state = {}
        with pytest.raises(ValueError):
            run(c, inputs, state)
        assert state == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("last_close", 999.0),
        ("last_decision_timestamp_ms", 0),
        ("last_trigger_key", "f" * 64),
        ("arming_evidence", [{"bad": True}]),
    ],
)
def test_checkpoint_data_integrity_refuses_changed_transition_inputs(field, value):
    state = {}
    evaluate(state, 0, event=True)
    state[field] = value
    before = deepcopy(state)
    with pytest.raises(ValueError):
        evaluate(state, 1)
    assert state == before


@pytest.mark.parametrize(
    "field,value",
    [("level_id", "forged"), ("source_hash", "0" * 64), ("source_node_id", "different-owner")],
)
def test_public_snapshot_schema_rejects_conflicting_frozen_identity(field, value):
    state = {}
    snapshot = evaluate(state, 0, event=True)["level"]
    with pytest.raises(ValueError):
        entities.LevelSnapshotV1.model_validate(snapshot.model_dump() | {field: value})


@pytest.mark.parametrize("field,value", [("trigger_key", "forged"), ("formula_version", "wrong")])
def test_binding_oracle_refuses_event_provenance_conflicts(field, value):
    from koval.strategy.graph.levels import level_event_binding

    state = {}
    evaluate(state, 0, event=True)
    result = evaluate(state, 1, ctx_changes={"open": 100.0, "low": 99.0, "close": 101.0})
    event = result["event"].model_copy(deep=True)
    event.metadata[field] = value
    assert not level_event_binding(
        context(1, open=100.0, low=99.0, close=101.0), result["level"], event
    )


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_missing_scalar_data_fingerprints_without_crashing_readiness(field):
    state = {}
    evaluate(state, 0, event=True)
    result = evaluate(
        state, 1, ctx_changes={"open": 100.0, "low": 99.0, "close": 101.0, field: None}
    )
    assert result["level"].status == ("consumed" if field == "volume" else "invalidated")
    assert (result["event"] is not None) == (field == "volume")


@pytest.mark.parametrize(
    "lower,upper,center",
    [
        (5e-324, 5e-324, 5e-324),
        (5e-324, 1e-323, 1e-323),
        (8.988465674311579e307, 1.7976931348623157e308, 1.3482698511467367e308),
    ],
)
def test_finite_positive_bounds_keep_a_finite_correct_midpoint(lower, upper, center):
    result = evaluate({}, 0, event=True, lower=lower, upper=upper)
    assert result["level"].status == "armed"
    assert result["level"].center == center
