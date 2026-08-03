"""Full graph-validation tests against the real NODE_CATALOG.

Note (plan deviation): the plan's draft edges used FACT nodes as connection
destinations, but FACT nodes have no input ports, so those edges fail the
port-existence check before reaching the domain/cycle rules. These tests
exercise the same rules with node/port combinations that actually exist:
domain-rank via EXECUTION->INTERPRETATION, type-incompat via
MarketEvent->`policies`, and a cycle via two co-rank INTERPRETATION nodes.
Cycle *detection* itself is additionally unit-tested in test_validation_cycle.py.
"""

import pytest

import koval.strategy.nodes  # noqa: F401 — populates NODE_CATALOG
from koval.strategy.block_assembler import GraphValidationError
from koval.strategy.graph.validation import validate_graph


def _edge(a, ap, b, bp):
    return {"from": a, "from_port": ap, "to": b, "to_port": bp}


def test_empty_graph_rejected():
    with pytest.raises(GraphValidationError, match="no blocks"):
        validate_graph({"blocks": [], "connections": []})


def test_unknown_node_type_rejected():
    graph = {"blocks": [{"id": "x", "type": "fact.nope"}], "connections": []}
    with pytest.raises(GraphValidationError, match="unknown type"):
        validate_graph(graph)


def test_duplicate_block_id_rejected():
    graph = {
        "blocks": [
            {"id": "f", "type": "fact.bos", "params": {}},
            {"id": "f", "type": "fact.choch", "params": {}},
        ],
        "connections": [],
    }
    with pytest.raises(GraphValidationError, match="Duplicate"):
        validate_graph(graph)


def test_invalid_params_rejected():
    # EmaCrossParams: fast must be < slow.
    graph = {
        "blocks": [{"id": "f", "type": "fact.ema_cross", "params": {"fast": 99, "slow": 9}}],
        "connections": [],
    }
    with pytest.raises(GraphValidationError, match="params invalid"):
        validate_graph(graph)


def test_connection_unknown_id_rejected():
    graph = {
        "blocks": [{"id": "f", "type": "fact.bos", "params": {}}],
        "connections": [_edge("f", "event", "ghost", "events")],
    }
    with pytest.raises(GraphValidationError, match="unknown id"):
        validate_graph(graph)


def test_missing_output_port_rejected():
    graph = {
        "blocks": [
            {"id": "f", "type": "fact.bos", "params": {}},
            {"id": "c", "type": "interp.confluence_and", "params": {}},
        ],
        "connections": [_edge("f", "nope", "c", "events")],
    }
    with pytest.raises(GraphValidationError, match="no output port"):
        validate_graph(graph)


def test_missing_input_port_rejected():
    graph = {
        "blocks": [
            {"id": "f", "type": "fact.bos", "params": {}},
            {"id": "c", "type": "interp.confluence_and", "params": {}},
        ],
        "connections": [_edge("f", "event", "c", "nope")],
    }
    with pytest.raises(GraphValidationError, match="no input port"):
        validate_graph(graph)


def test_domain_rank_violation_rejected():
    # EXECUTION output feeding an INTERPRETATION input is forbidden (rank rule).
    graph = {
        "blocks": [
            {"id": "o", "type": "exec.order_constructor", "params": {}},
            {"id": "c", "type": "interp.confluence_and", "params": {}},
        ],
        "connections": [_edge("o", "order", "c", "events")],
    }
    with pytest.raises(GraphValidationError, match="domain"):
        validate_graph(graph)


def test_type_incompatible_port_rejected():
    graph = {
        "blocks": [
            {"id": "f", "type": "fact.bos", "params": {}},
            {"id": "g", "type": "interp.direction_gate", "params": {}},
        ],
        # direction_gate.policies expects PolicyDecision, not a MarketEvent
        "connections": [_edge("f", "event", "g", "policies")],
    }
    with pytest.raises(GraphValidationError, match="type"):
        validate_graph(graph)


def test_unconnected_required_input_port_rejected():
    graph = {
        "blocks": [{"id": "c", "type": "interp.confluence_and", "params": {}}],
        "connections": [],
    }
    with pytest.raises(GraphValidationError, match=r"required input.*c\.events"):
        validate_graph(graph)


def test_duplicate_connection_rejected():
    connection = _edge("f", "event", "c", "events")
    graph = {
        "blocks": [
            {"id": "f", "type": "fact.bos", "params": {}},
            {"id": "c", "type": "interp.confluence_and", "params": {}},
        ],
        "connections": [connection, dict(connection)],
    }
    with pytest.raises(GraphValidationError, match="Duplicate connection"):
        validate_graph(graph)


def test_multiple_connections_to_single_input_rejected():
    graph = {
        "blocks": [
            {"id": "a", "type": "fact.bos", "params": {}},
            {"id": "b", "type": "fact.choch", "params": {}},
            {"id": "g", "type": "interp.direction_gate", "params": {}},
        ],
        "connections": [
            _edge("a", "event", "g", "agreement"),
            _edge("b", "event", "g", "agreement"),
        ],
    }
    with pytest.raises(GraphValidationError, match=r"g\.agreement.*at most 1"):
        validate_graph(graph)


def test_declared_fan_in_accepts_multiple_connections():
    graph = {
        "blocks": [
            {"id": "a", "type": "fact.bos", "params": {}},
            {"id": "b", "type": "fact.choch", "params": {}},
            {"id": "c", "type": "interp.confluence_and", "params": {}},
        ],
        "connections": [
            _edge("a", "event", "c", "events"),
            _edge("b", "event", "c", "events"),
        ],
    }
    validate_graph(graph)


def test_fact_can_feed_state_context():
    graph = {
        "blocks": [
            {"id": "f", "type": "fact.bos", "params": {}},
            {"id": "s", "type": "state.trend_bias", "params": {}},
        ],
        "connections": [_edge("f", "event", "s", "context")],
    }
    validate_graph(graph)  # must not raise


def test_fact_can_feed_policy_context():
    graph = {
        "blocks": [
            {"id": "f", "type": "fact.bos", "params": {}},
            {"id": "p", "type": "policy.rsi", "params": {}},
        ],
        "connections": [_edge("f", "event", "p", "context")],
    }
    validate_graph(graph)  # must not raise


def test_cycle_rejected():
    # Two co-rank INTERPRETATION nodes wired into a 2-cycle; every edge is
    # individually valid (MarketEvent -> events), so only the DAG check fires.
    graph = {
        "blocks": [
            {"id": "a", "type": "interp.confluence_and", "params": {}},
            {"id": "b", "type": "interp.confluence_and", "params": {}},
        ],
        "connections": [
            _edge("a", "agreement", "b", "events"),
            _edge("b", "agreement", "a", "events"),
        ],
    }
    with pytest.raises(GraphValidationError, match="cycle"):
        validate_graph(graph)


def test_valid_native_graph_passes():
    graph = {
        "blocks": [
            {"id": "sig", "type": "fact.ema_cross", "params": {"fast": 9, "slow": 21}},
            {"id": "conf", "type": "interp.confluence_and", "params": {}},
            {"id": "gate", "type": "interp.direction_gate", "params": {}},
            {"id": "ord", "type": "exec.order_constructor", "params": {}},
        ],
        "connections": [
            _edge("sig", "event", "conf", "events"),
            _edge("conf", "agreement", "gate", "agreement"),
            _edge("gate", "intent", "ord", "intent"),
        ],
    }
    validate_graph(graph)  # must not raise
