"""Causal numeric primitives, independently of any execution plugin."""

import importlib
import importlib.util
from dataclasses import replace

import numpy as np
import pytest
from pydantic import ValidationError

from koval.strategy.graph import entities
from koval.strategy.graph.node import BarContext
from koval.strategy.helpers._math import _atr, _ema
from koval.strategy.helpers.filters.volatility import bb_volatility_filter


@pytest.fixture
def api():
    assert hasattr(entities, "NumericValueV1"), "NumericValueV1 public contract is missing"
    assert hasattr(entities, "ConditionV1"), "ConditionV1 public contract is missing"
    assert importlib.util.find_spec("koval.strategy.graph.values"), "value_at is missing"
    return importlib.import_module("koval.strategy.graph.values")


def context(closes=(1, 2, 3), **overrides):
    a = np.array(closes, dtype=float)
    values = dict(
        close=float(a[-1]),
        high=float(a[-1] + 1),
        low=float(a[-1] - 1),
        open=float(a[-1]),
        volume=10,
        bar_index=len(a) - 1,
        timestamp_ms=(len(a) - 1) * 60_000,
        closes=a,
        highs=a + 1,
        lows=a - 1,
        opens=a.copy(),
        volumes=np.full(len(a), 10.0),
        symbol="BTCUSDT",
        timeframe="1m",
        history_start_ms=0,
        decision_timestamp_ms=len(a) * 60_000,
    )
    return BarContext(**(values | overrides))


def value(api, component, ctx=None, **params):
    return api.value_at(ctx or context(), api.ValueSpecV1(component=component, **params))


def test_bollinger_population_bands_and_units(api):
    sigma = np.sqrt(2 / 3)
    for component, expected, unit in [
        ("bb.middle", 2, "price_quote_per_base"),
        ("bb.upper", 2 + 2 * sigma, "price_quote_per_base"),
        ("bb.lower", 2 - 2 * sigma, "price_quote_per_base"),
        ("bb.width_pct", (4 * sigma) / 2 * 100, "percent"),
        ("bb.zscore", 1 / sigma, "dimensionless"),
    ]:
        result = value(api, component, period=3, std_dev=2.0)
        assert result.value == pytest.approx(expected)
        assert result.unit == unit
        assert result.readiness == "ready"
        assert result.as_of_ms == result.available_at_ms == 180_000
        assert result.formula_version and result.input_cursor


def test_flat_zscore_and_zero_middle_are_unavailable(api):
    result = value(api, "bb.zscore", context((2, 2, 2)), period=3)
    assert (result.value, result.readiness, result.reason) == (None, "missing", "zero_variance")
    result = value(api, "bb.width_pct", context((-1, 0, 1)), period=3)
    assert (result.value, result.readiness, result.reason) == (None, "invalid", "zero_middle")


def test_previous_channel_excludes_current_high_and_low(api):
    ctx = context(
        (10, 11, 12, 100), highs=np.array([10, 11, 12, 100.0]), lows=np.array([8, 9, 10, -100.0])
    )
    for component, expected in [
        ("channel.upper", 12),
        ("channel.lower", 8),
        ("channel.center", 10),
    ]:
        result = value(api, component, ctx, period=3)
        assert result.value == expected
        assert result.as_of_ms == 180_000


@pytest.mark.parametrize(
    "component,unit,expected",
    [
        ("open", "price_quote_per_base", 3),
        ("high", "price_quote_per_base", 4),
        ("low", "price_quote_per_base", 2),
        ("close", "price_quote_per_base", 3),
        ("volume", "base_volume", 10),
    ],
)
def test_confirmed_ohlcv(api, component, unit, expected):
    result = value(api, component)
    assert (result.value, result.unit) == (expected, unit)


def test_ema_atr_reuse_existing_seed_and_bounded_prefix_with_offset(api):
    ctx = context(tuple(range(1, 35)))
    for offset in (0, 1, 7):
        end = len(ctx.closes) - offset
        assert (
            value(api, "ema", ctx, period=3, offset_bars=offset).value
            == _ema(ctx.closes[:end], 3)[-1]
        )
        assert value(api, "atr", ctx, period=3, offset_bars=offset).value == _atr(
            ctx.highs[:end], ctx.lows[:end], ctx.closes[:end], 3
        )
        assert value(api, "close", ctx, offset_bars=offset).as_of_ms == end * 60_000


@pytest.mark.parametrize(
    "component,params,minimum",
    [
        ("close", {"offset_bars": 500}, 501),
        ("ema", {"period": 400, "offset_bars": 20}, 420),
        ("atr", {"period": 400, "offset_bars": 20}, 421),
        ("channel.upper", {"period": 400, "offset_bars": 20}, 421),
    ],
)
def test_composite_history_requirement_is_not_clamped(api, component, params, minimum):
    spec = api.ValueSpecV1(component=component, **params)
    assert spec.required_history_bars() == minimum
    assert api.value_at(context(), spec).readiness == "warming_up"


@pytest.mark.parametrize(
    "params",
    [
        {"component": "adx"},
        {"component": "close", "period": 3},
        {"component": "ema", "std_dev": 2},
        {"component": "ema", "period": 1},
        {"component": "ema", "period": 401},
        {"component": "close", "offset_bars": -1},
        {"component": "close", "context": "htf"},
        {"component": "close", "expression": "1+1"},
        {"component": "bb.upper", "std_dev": float("inf")},
        {"component": "bb.upper", "std_dev": 0},
    ],
)
def test_value_variants_reject_unsupported_fields(api, params):
    with pytest.raises(ValidationError):
        api.ValueSpecV1(**params)


@pytest.mark.parametrize(
    "changes,readiness,reason",
    [
        ({"closes": None}, "missing", "missing_closes"),
        ({"closes": np.array([1.0, np.nan, 3.0])}, "invalid", "nonfinite_input"),
        ({"decision_timestamp_ms": None}, "missing", "missing_timing"),
        ({"history_start_ms": None}, "missing", "missing_timing"),
        ({"timeframe": None}, "missing", "missing_identity"),
        ({"symbol": ""}, "missing", "missing_identity"),
        ({"decision_timestamp_ms": 179_999}, "missing", "future_availability"),
        ({"history_start_ms": 1}, "invalid", "noncontiguous_history"),
        ({"decision_timestamp_ms": 240_000}, "stale", "stale_primary_bar"),
    ],
)
def test_readiness_never_fakes_a_value(api, changes, readiness, reason):
    result = value(api, "ema", replace(context(), **changes), period=2)
    assert result.value is None
    assert (result.readiness, result.reason) == (readiness, reason)


def test_future_suffix_cannot_change_prefix_or_offset(api):
    rows = (1, 4, 2, 6, 3, 10000)
    for component in ("ema", "atr", "bb.upper", "channel.upper"):
        spec = api.ValueSpecV1(component=component, period=2)
        first = api.value_at(context(rows[:-1]), spec)
        again = api.value_at(context(rows[:-1]), spec)
        assert first == again
        assert (
            first.value
            == api.value_at(
                context(rows), api.ValueSpecV1(component=component, period=2, offset_bars=1)
            ).value
        )


@pytest.mark.parametrize("closes", [(1, 2, 3), (2, 2, 2), (-1, 0, 1), (-3, -2, -1)])
def test_legacy_bb_predicate_keeps_original_arithmetic(closes):
    a = np.asarray(closes, dtype=float)
    mid, sigma = float(a.mean()), float(a.std(ddof=0))
    for threshold in (0, 1, 100, 200):
        expected = (
            False
            if mid == 0
            else ((mid + 2 * sigma) - (mid - 2 * sigma)) / mid * 100.0 >= threshold
        )
        assert bb_volatility_filter(a, 3, 2, threshold) == expected


def test_entities_enforce_ready_and_unavailable_contract(api):
    ready = value(api, "close")
    for patch in (
        {"value": None},
        {"value": float("nan")},
        {"value": float("inf")},
        {"readiness": "missing"},
        {"available_at_ms": None},
        {"symbol": ""},
    ):
        with pytest.raises(ValidationError):
            type(ready).model_validate(ready.model_dump() | patch)
    unavailable = value(api, "ema", period=20)
    assert unavailable.value is None
    with pytest.raises(ValidationError):
        type(unavailable).model_validate(unavailable.model_dump() | {"value": 0.0})


def test_value_contract_is_public():
    assert hasattr(entities, "NumericValueV1"), "NumericValueV1 must be publicly importable"
    assert hasattr(entities, "ConditionV1"), "ConditionV1 must be publicly importable"


def test_formula_identity_changes_with_component_and_parameters(api):
    assert (
        value(api, "ema", period=2).formula_version != value(api, "ema", period=3).formula_version
    )
    assert value(api, "ema", period=3).formula_version != value(api, "close").formula_version


def test_channel_with_unsupported_timeframe_is_unavailable(api):
    result = value(api, "channel.upper", context(timeframe="unknown"), period=2)
    assert (result.value, result.readiness, result.reason) == (
        None,
        "invalid",
        "unsupported_timeframe",
    )


def test_bb_overflowing_variance_never_becomes_ready_zero_zscore(api):
    result = value(api, "bb.zscore", context((-1e200, 1e200)), period=2)
    assert (result.value, result.readiness, result.reason) == (
        None,
        "invalid",
        "nonfinite_intermediate",
    )


def test_channel_excluded_bar_does_not_change_readiness(api):
    ctx = context((1, 2, 3), highs=np.array([2.0, 3.0, np.nan]), lows=np.array([0.0, 1.0, np.nan]))
    for component, expected in (
        ("channel.upper", 3.0),
        ("channel.lower", 0.0),
        ("channel.center", 1.5),
    ):
        result = value(api, component, ctx, period=2)
        assert (result.value, result.readiness) == (expected, "ready")
