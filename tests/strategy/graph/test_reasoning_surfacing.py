import numpy as np

import koval.strategy.nodes  # noqa: F401
from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import TradingIntent
from koval.strategy.graph.node import NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import NODE_CATALOG, register_node
from koval.strategy.graph.strategy import build_graph_strategy
from koval.strategy.schemas import _StrictModel


class _IntentParams(_StrictModel):
    reasoning: str | None = None


def _intent_factory(p):
    def evaluate(ctx, inputs, state):
        meta = {"reasoning_chain": p.reasoning} if p.reasoning is not None else {}
        return {
            "intent": TradingIntent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="test_intent",
                side="buy",
                metadata=meta,
            )
        }

    return evaluate


def _register_intent():
    register_node(
        NodeSpec(
            "test.fixed_intent",
            Domain.INTERPRETATION,
            "fixed",
            "",
            _IntentParams,
            {},
            {"intent": PortSpec("intent", (TradingIntent,))},
            _intent_factory,
        )
    )


def teardown_function():
    NODE_CATALOG.pop("test.fixed_intent", None)


def _graph(reasoning):
    params = {"reasoning": reasoning} if reasoning is not None else {}
    return {
        "blocks": [
            {"id": "intent", "type": "test.fixed_intent", "params": params},
            {
                "id": "order",
                "type": "exec.order_constructor",
                "params": {"sl_pct": 2.0, "risk_reward": 2.0, "risk_pct": 1.0},
            },
        ],
        "connections": [
            {"from": "intent", "from_port": "intent", "to": "order", "to_port": "intent"},
        ],
    }


def _drive(strat):
    # DeclarativeStrategy fields are injected by direct attribute assignment
    # (mirrors the adapter; see tests/strategy/graph/test_graph_strategy.py).
    strat.close = 100.0
    strat.high = 101.0
    strat.low = 99.0
    strat.open = 100.0
    strat.volume = 1.0
    strat.bar_index = 0
    strat.timestamp_ms = 0
    strat.closes = np.array([100.0])
    strat.account_value = 10_000.0


def test_reasoning_chain_surfaces_into_why_entry():
    _register_intent()
    strat = build_graph_strategy(_graph("Gate PASS 45>=40"))()
    _drive(strat)
    assert strat.should_long() is True
    setup = strat.go_long()
    assert setup.why_entry == ["Gate PASS 45>=40"]


def test_intent_without_reasoning_leaves_why_entry_empty():
    _register_intent()
    strat = build_graph_strategy(_graph(None))()
    _drive(strat)
    assert strat.should_long() is True
    setup = strat.go_long()
    assert setup.why_entry == []
