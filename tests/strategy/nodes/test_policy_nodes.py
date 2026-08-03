import numpy as np

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
