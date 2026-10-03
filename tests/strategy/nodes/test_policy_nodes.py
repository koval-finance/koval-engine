import numpy as np
import pytest
from pydantic import ValidationError

import koval.strategy.nodes  # noqa: F401
from koval.strategy.graph.entities import PolicyDecision
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.registry import get_node


def _ctx(closes):
    return BarContext(
        close=float(closes[-1]),
        high=float(closes[-1]),
        low=float(closes[-1]),
        open=float(closes[-1]),
        volume=1.0,
        bar_index=len(closes) - 1,
        timestamp_ms=1000,
        closes=np.array(closes, float),
    )


def test_policy_rsi_allows_inside_band():
    spec = get_node("policy.rsi")
    ev = spec.factory(spec.params_schema(period=2, min_val=0.0, max_val=100.0))
    out = ev(_ctx([1, 2, 3, 4, 5]), {}, {})
    p = out["policy"]
    assert isinstance(p, PolicyDecision)
    assert p.allowed is True


def test_policy_rsi_blocks_outside_band_with_reason():
    spec = get_node("policy.rsi")
    # Strong uptrend -> RSI near 100; band [0, 10] -> blocked.
    ev = spec.factory(spec.params_schema(period=2, min_val=0.0, max_val=10.0))
    out = ev(_ctx([1, 2, 3, 4, 5]), {}, {})
    p = out["policy"]
    assert p.allowed is False
    assert p.reason == "rsi_out_of_band"


def _cooldown(bars=2):
    spec = get_node("policy.cooldown")
    return spec.factory(spec.params_schema(bars=bars))


def _cooldown_step(ev, state, position_size):
    ctx = _ctx([1, 2, 3])
    ctx.position_size = position_size
    return ev(ctx, {}, state)["policy"]


def test_policy_cooldown_allows_before_any_position():
    ev, state = _cooldown(), {}
    decisions = [_cooldown_step(ev, state, 0.0) for _ in range(5)]
    assert all(isinstance(p, PolicyDecision) and p.allowed for p in decisions)
    assert all(p.reason is None for p in decisions)


def test_policy_cooldown_blocks_while_a_position_is_open():
    ev, state = _cooldown(), {}
    for size in (1.5, -1.5):
        p = _cooldown_step(ev, state, size)
        assert p.allowed is False
        assert p.reason == "cooldown_active"


@pytest.mark.parametrize("bars", [1, 2, 5])
def test_policy_cooldown_blocks_exactly_bars_flat_bars_then_allows(bars):
    ev, state = _cooldown(bars), {}
    _cooldown_step(ev, state, 1.0)
    flat = [_cooldown_step(ev, state, 0.0) for _ in range(bars + 2)]
    assert [p.allowed for p in flat] == [False] * bars + [True, True]
    assert [p.reason for p in flat] == ["cooldown_active"] * bars + [None, None]


def test_policy_cooldown_reentry_during_cooldown_resets_the_count():
    ev, state = _cooldown(3), {}
    _cooldown_step(ev, state, 1.0)
    assert not _cooldown_step(ev, state, 0.0).allowed
    assert not _cooldown_step(ev, state, 0.0).allowed
    _cooldown_step(ev, state, 1.0)
    flat = [_cooldown_step(ev, state, 0.0).allowed for _ in range(4)]
    assert flat == [False, False, False, True]


def test_policy_cooldown_keeps_state_per_instance():
    spec = get_node("policy.cooldown")
    ev = spec.factory(spec.params_schema(bars=2))
    held, fresh = {}, {}
    _cooldown_step(ev, held, 1.0)
    assert _cooldown_step(ev, held, 0.0).allowed is False
    assert _cooldown_step(ev, fresh, 0.0).allowed is True


def test_policy_cooldown_rejects_non_positive_bars():
    spec = get_node("policy.cooldown")
    for bars in (0, -1):
        with pytest.raises(ValidationError):
            spec.params_schema(bars=bars)
    with pytest.raises(ValidationError):
        spec.params_schema(bars=1, extra=1)


def _atr_ctx(half_range=1.0, n=30):
    # Constant true range 2 * half_range on a flat 100 close: atr_pct == 2 * half_range.
    closes = np.full(n, 100.0)
    ctx = _ctx(closes)
    ctx.highs, ctx.lows = closes + half_range, closes - half_range
    return ctx


def _atr_policy(ctx, **params):
    spec = get_node("policy.atr_volatility")
    return spec.factory(spec.params_schema(**params))(ctx, {}, {})["policy"]


def test_policy_atr_volatility_blocks_above_the_cap_with_its_own_reason():
    p = _atr_policy(_atr_ctx(1.0), min_atr_pct=0, max_atr_pct=1.5)
    assert p.allowed is False
    assert p.reason == "volatility_too_high"


def test_policy_atr_volatility_allows_at_the_cap():
    p = _atr_policy(_atr_ctx(1.0), min_atr_pct=0, max_atr_pct=2.0)
    assert p.allowed is True
    assert p.reason is None


def test_policy_atr_volatility_band_blocks_both_sides():
    band = {"min_atr_pct": 1.0, "max_atr_pct": 3.0}
    assert _atr_policy(_atr_ctx(1.0), **band).allowed is True
    low = _atr_policy(_atr_ctx(0.25), **band)
    assert (low.allowed, low.reason) == (False, "volatility_too_low")
    high = _atr_policy(_atr_ctx(2.0), **band)
    assert (high.allowed, high.reason) == (False, "volatility_too_high")


def test_policy_atr_volatility_floor_reason_is_unchanged_without_a_cap():
    low = _atr_policy(_atr_ctx(0.1), min_atr_pct=1.0)
    assert (low.allowed, low.reason) == (False, "volatility_too_low")
    short = _atr_policy(_atr_ctx(1.0, n=3), min_atr_pct=0, max_atr_pct=5.0)
    assert (short.allowed, short.reason) == (False, "volatility_too_low")
