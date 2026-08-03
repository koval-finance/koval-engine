import numpy as np
import pytest
from pydantic import BaseModel

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import MarketEvent, OrderRequest, TradingIntent
from koval.strategy.graph.executor import GraphExecutor
from koval.strategy.graph.node import BarContext, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import NODE_CATALOG, register_node


class _P(BaseModel):
    pass


def _counter_factory(_p):
    # STATE-style node: counts how many bars it has seen, via NodeState.
    def evaluate(ctx, inputs, state):
        state["seen"] = state.get("seen", 0) + 1
        return {
            "event": MarketEvent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="c",
                kind="tick",
                strength=float(state["seen"]),
            )
        }

    return evaluate


def _intent_factory(_p):
    def evaluate(ctx, inputs, state):
        evs = inputs.get("events", [])
        if not evs:
            return {"intent": None}
        return {
            "intent": TradingIntent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="i",
                side="buy",
            )
        }

    return evaluate


def _ctx(i):
    return BarContext(
        close=1.0,
        high=1.0,
        low=1.0,
        open=1.0,
        volume=1.0,
        bar_index=i,
        timestamp_ms=i * 1000,
        closes=np.array([1.0]),
    )


def _register_fakes():
    register_node(
        NodeSpec(
            "test.counter",
            Domain.FACT,
            "c",
            "",
            _P,
            {},
            {"event": PortSpec("event", (MarketEvent,))},
            _counter_factory,
        )
    )
    register_node(
        NodeSpec(
            "test.intent",
            Domain.INTERPRETATION,
            "i",
            "",
            _P,
            {"events": PortSpec("events", (MarketEvent,))},
            {"intent": PortSpec("intent", (TradingIntent,))},
            _intent_factory,
        )
    )


def teardown_function():
    NODE_CATALOG.pop("test.counter", None)
    NODE_CATALOG.pop("test.intent", None)
    NODE_CATALOG.pop("test.bad_type", None)
    NODE_CATALOG.pop("test.bad_port", None)


def test_executor_runs_topologically_and_persists_state():
    _register_fakes()
    graph = {
        "blocks": [
            {"id": "c", "type": "test.counter", "params": {}},
            {"id": "i", "type": "test.intent", "params": {}},
        ],
        "connections": [
            {"from": "c", "from_port": "event", "to": "i", "to_port": "events"},
        ],
    }
    ex = GraphExecutor.build(graph)
    ex.step(_ctx(0))
    r2 = ex.step(_ctx(1))
    # state persisted across bars
    assert r2.entities_by_node["c"]["event"].strength == 2.0
    # downstream consumed upstream output this bar
    assert any(isinstance(i, TradingIntent) for i in r2.intents)


def _draft_order_factory(_p):
    from koval.strategy.graph.entities import OrderRequest

    def evaluate(ctx, inputs, state):
        return {
            "order": OrderRequest(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="draft",
                symbol="",
                side="buy",
                entry_price=1.0,
                stop_price=0.9,
                quantity=0.0,
            )
        }

    return evaluate


def _passthrough_order_factory(_p):
    def evaluate(ctx, inputs, state):
        orders = inputs.get("order", [])
        if not orders:
            return {"order": None}
        o = orders[0]
        return {"order": o.model_copy(update={"quantity": 5.0, "source_node_id": "final"})}

    return evaluate


def test_executor_collects_only_terminal_orders():
    from koval.strategy.graph.node import NodeSpec as _NodeSpec

    register_node(
        _NodeSpec(
            "test.draft",
            Domain.EXECUTION,
            "d",
            "",
            _P,
            {},
            {"order": PortSpec("order", (OrderRequest,))},
            _draft_order_factory,
        )
    )
    register_node(
        _NodeSpec(
            "test.final",
            Domain.EXECUTION,
            "f",
            "",
            _P,
            {"order": PortSpec("order", (OrderRequest,))},
            {"order": PortSpec("order", (OrderRequest,), terminal=True)},
            _passthrough_order_factory,
        )
    )
    try:
        graph = {
            "blocks": [
                {"id": "d", "type": "test.draft", "params": {}},
                {"id": "f", "type": "test.final", "params": {}},
            ],
            "connections": [
                {"from": "d", "from_port": "order", "to": "f", "to_port": "order"},
            ],
        }
        ex = GraphExecutor.build(graph)
        r = ex.step(_ctx(0))
        # Only the terminal (unconsumed) order is collected, with qty=5.0.
        assert len(r.orders) == 1
        assert r.orders[0].quantity == 5.0
        assert r.orders[0].source_node_id == "f"
    finally:
        NODE_CATALOG.pop("test.draft", None)
        NODE_CATALOG.pop("test.final", None)


def test_executor_does_not_route_unconsumed_non_terminal_order():
    register_node(
        NodeSpec(
            "test.draft",
            Domain.EXECUTION,
            "draft",
            "",
            _P,
            {},
            {"order": PortSpec("order", (OrderRequest,))},
            _draft_order_factory,
        )
    )
    try:
        graph = {
            "blocks": [{"id": "d", "type": "test.draft", "params": {}}],
            "connections": [],
        }
        result = GraphExecutor.build(graph).step(_ctx(0))
        assert result.orders == []
    finally:
        NODE_CATALOG.pop("test.draft", None)


def _bad_type_factory(_p):
    def evaluate(ctx, inputs, state):
        return {
            "event": OrderRequest(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="bad",
                symbol="BTCUSDT",
                side="buy",
                entry_price=1.0,
                stop_price=0.9,
                quantity=1.0,
            )
        }

    return evaluate


def _bad_port_factory(_p):
    def evaluate(ctx, inputs, state):
        return {
            "ghost": MarketEvent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="bad",
                kind="bad_port",
            )
        }

    return evaluate


def test_executor_rejects_entity_not_declared_for_output_port():
    register_node(
        NodeSpec(
            "test.bad_type",
            Domain.FACT,
            "bad",
            "",
            _P,
            {},
            {"event": PortSpec("event", (MarketEvent,))},
            _bad_type_factory,
        )
    )
    graph = {
        "blocks": [{"id": "bad", "type": "test.bad_type", "params": {}}],
        "connections": [],
    }
    with pytest.raises(RuntimeError, match=r"bad\.event.*OrderRequest.*MarketEvent"):
        GraphExecutor.build(graph).step(_ctx(0))


def test_executor_rejects_undeclared_output_port():
    register_node(
        NodeSpec(
            "test.bad_port",
            Domain.FACT,
            "bad",
            "",
            _P,
            {},
            {"event": PortSpec("event", (MarketEvent,))},
            _bad_port_factory,
        )
    )
    graph = {
        "blocks": [{"id": "bad", "type": "test.bad_port", "params": {}}],
        "connections": [],
    }
    with pytest.raises(RuntimeError, match=r"bad.*undeclared output port.*ghost"):
        GraphExecutor.build(graph).step(_ctx(0))
