import numpy as np
import pytest

import koval.strategy.nodes  # noqa: F401
from koval.strategy.block_assembler import GraphValidationError
from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import (
    MarketEvent,
    OrderRequest,
    TradingIntent,
)
from koval.strategy.graph.executor import GraphExecutor
from koval.strategy.graph.node import BarContext, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import NODE_CATALOG, register_node
from koval.strategy.graph.validation import validate_graph
from koval.strategy.schemas import _StrictModel

_INTERPRETATION_CHAIN_NODES = [
    "interp.event_aggregator",
    "interp.setup_generator",
    "interp.setup_score",
    "interp.context_score",
    "interp.qualification_gate",
    "interp.signal_emitter",
]


# ---------------------------------------------------------------------------
# Catalog and native-chain validation
# ---------------------------------------------------------------------------


def test_all_chain_nodes_registered_in_interpretation_domain():
    for t in _INTERPRETATION_CHAIN_NODES:
        assert t in NODE_CATALOG, t
        assert NODE_CATALOG[t].domain is Domain.INTERPRETATION


def test_aggregator_port_shapes():
    spec = NODE_CATALOG["interp.event_aggregator"]
    assert "events" in spec.input_ports
    assert "candidate" in spec.output_ports


def _valid_chain() -> dict:
    return {
        "blocks": [
            {"id": "ob", "type": "fact.ob", "params": {}},
            {
                "id": "agg",
                "type": "interp.event_aggregator",
                "params": {"event_sequence": ["order_block"]},
            },
            {"id": "gen", "type": "interp.setup_generator", "params": {}},
            {"id": "ss", "type": "interp.setup_score", "params": {}},
            {"id": "gate", "type": "interp.qualification_gate", "params": {}},
            {"id": "emit", "type": "interp.signal_emitter", "params": {}},
            {"id": "order", "type": "exec.order_constructor", "params": {}},
        ],
        "connections": [
            {"from": "ob", "from_port": "event", "to": "agg", "to_port": "events"},
            {"from": "agg", "from_port": "candidate", "to": "gen", "to_port": "candidate"},
            {"from": "gen", "from_port": "candidate", "to": "ss", "to_port": "candidate"},
            {"from": "ss", "from_port": "scored", "to": "gate", "to_port": "setup_score"},
            {"from": "gate", "from_port": "scored", "to": "emit", "to_port": "scored"},
            {"from": "gen", "from_port": "candidate", "to": "emit", "to_port": "candidate"},
            {"from": "emit", "from_port": "intent", "to": "order", "to_port": "intent"},
        ],
    }


def test_valid_interp_chain_passes_validation():
    validate_graph(_valid_chain())  # must not raise


def test_type_mismatch_edge_is_rejected():
    graph = _valid_chain()
    # feed a SetupCandidate where a ScoredSetup is expected
    graph["connections"].append(
        {"from": "gen", "from_port": "candidate", "to": "gate", "to_port": "setup_score"}
    )
    with pytest.raises(GraphValidationError):
        validate_graph(graph)


# ---------------------------------------------------------------------------
# Deterministic executor-level chain integration
# ---------------------------------------------------------------------------


class _ScriptParams(_StrictModel):
    script: dict[str, list[str]] = {}  # str(bar_index) -> [kind, direction]


def _scripted_factory(p):
    def evaluate(ctx, inputs, state):
        entry = p.script.get(str(ctx.bar_index))
        if not entry:
            return {"event": None}
        kind, direction = entry
        return {
            "event": MarketEvent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="scripted",
                kind=kind,
                direction=direction,
            )
        }

    return evaluate


def _register_scripted():
    register_node(
        NodeSpec(
            "test.scripted_fact",
            Domain.FACT,
            "scripted",
            "",
            _ScriptParams,
            {},
            {"event": PortSpec("event", (MarketEvent,))},
            _scripted_factory,
        )
    )


def teardown_function():
    NODE_CATALOG.pop("test.scripted_fact", None)


def _bar(i: int) -> BarContext:
    return BarContext(
        close=100.0,
        high=101.0,
        low=99.0,
        open=100.0,
        volume=1.0,
        bar_index=i,
        timestamp_ms=i * 1000,
        closes=np.full(i + 1, 100.0),
        account_value=10_000.0,
    )


def _scripted_chain() -> dict:
    return {
        "blocks": [
            {
                "id": "sf",
                "type": "test.scripted_fact",
                "params": {"script": {"3": ["order_block", "bullish"], "7": ["choch", "bullish"]}},
            },
            {
                "id": "agg",
                "type": "interp.event_aggregator",
                "params": {"event_sequence": ["order_block", "choch"], "timeout_bars": 20},
            },
            {
                "id": "gen",
                "type": "interp.setup_generator",
                "params": {"setup_type_map": {"order_block->choch": "ob_reversal"}},
            },
            {
                "id": "ss",
                "type": "interp.setup_score",
                "params": {"points": {"order_block": 20, "choch": 25}},
            },
            {"id": "cs", "type": "interp.context_score", "params": {"points": {}}},
            {
                "id": "gate",
                "type": "interp.qualification_gate",
                "params": {"qualification_threshold": 40},
            },
            {"id": "emit", "type": "interp.signal_emitter", "params": {}},
            {
                "id": "order",
                "type": "exec.order_constructor",
                "params": {"sl_pct": 2.0, "risk_reward": 2.0, "risk_pct": 1.0},
            },
        ],
        "connections": [
            {"from": "sf", "from_port": "event", "to": "agg", "to_port": "events"},
            {"from": "agg", "from_port": "candidate", "to": "gen", "to_port": "candidate"},
            {"from": "gen", "from_port": "candidate", "to": "ss", "to_port": "candidate"},
            {"from": "gen", "from_port": "candidate", "to": "cs", "to_port": "candidate"},
            {"from": "ss", "from_port": "scored", "to": "gate", "to_port": "setup_score"},
            {"from": "cs", "from_port": "scored", "to": "gate", "to_port": "context_score"},
            {"from": "gate", "from_port": "scored", "to": "emit", "to_port": "scored"},
            {"from": "gen", "from_port": "candidate", "to": "emit", "to_port": "candidate"},
            {"from": "emit", "from_port": "intent", "to": "order", "to_port": "intent"},
        ],
    }


def test_scripted_chain_qualifies_and_emits_intent_with_reasoning():
    _register_scripted()
    ex = GraphExecutor.build(_scripted_chain())
    results = [ex.step(_bar(i)) for i in range(9)]
    # nothing before the sequence completes on bar 7
    assert all(not r.intents for r in results[:7])
    final = results[7]
    assert len(final.intents) == 1
    intent = final.intents[0]
    assert isinstance(intent, TradingIntent)
    assert intent.side == "buy"
    assert intent.metadata["reasoning_chain"] == (
        "Setup 45 (order_block +20, choch +25); Context 0; Gate PASS 45>=40"
    )
    assert any(isinstance(o, OrderRequest) for o in final.orders)
