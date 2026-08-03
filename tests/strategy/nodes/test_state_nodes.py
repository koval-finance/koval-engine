import numpy as np

import koval.strategy.nodes  # noqa: F401
from koval.strategy.graph.entities import MarketState
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.registry import get_node


def _ctx(closes, i):
    return BarContext(
        close=float(closes[-1]),
        high=float(closes[-1]),
        low=float(closes[-1]),
        open=float(closes[-1]),
        volume=1.0,
        bar_index=i,
        timestamp_ms=i * 1000,
        closes=np.array(closes, float),
    )


def test_trend_bias_emits_state_and_tracks_since_bar():
    spec = get_node("state.trend_bias")
    ev = spec.factory(spec.params_schema(period=3))
    state = {}
    up = [1, 2, 3, 4, 5]
    s1 = ev(_ctx(up, 4), {}, state)["state"]
    assert isinstance(s1, MarketState)
    assert s1.kind == "trend_bias"
    assert s1.status in {"bullish", "bearish", "neutral"}
    since_first = s1.since_bar
    s2 = ev(_ctx([1, 2, 3, 4, 5, 6], 5), {}, state)["state"]
    # status unchanged -> since_bar stays pinned to the first bar of this status
    if s2.status == s1.status:
        assert s2.since_bar == since_first


def test_volatility_regime_guards_missing_highs():
    spec = get_node("state.volatility_regime")
    ev = spec.factory(spec.params_schema(period=3))
    # closes only, no highs/lows -> must not crash, returns a normal regime
    s = ev(_ctx([1, 2, 3, 4, 5], 4), {}, {})["state"]
    assert isinstance(s, MarketState)
    assert s.kind == "volatility_regime"
    assert s.status in {"normal", "expansion", "compression"}
