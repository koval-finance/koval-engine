import koval.strategy.nodes  # noqa: F401
from koval.strategy.graph.compat import compile_legacy, is_legacy_graph
from koval.strategy.graph.validation import validate_graph

LEGACY = {
    "blocks": [
        {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 9, "slow": 21}},
        {"id": "flt", "type": "filter.ema_trend", "params": {"period": 50, "direction": "bullish"}},
        {"id": "ent", "type": "entry.long_only", "params": {"entry_type": "market"}},
        {"id": "ext", "type": "exit.fixed_sl_tp", "params": {"sl_pct": 2.0, "risk_reward": 2.0}},
        {"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1.0, "leverage": 1.0}},
    ],
    "connections": [
        {"from": "sig", "to": "ent"},
        {"from": "flt", "to": "ent"},
        {"from": "ent", "to": "ext"},
        {"from": "ext", "to": "rsk"},
    ],
}


def test_is_legacy_graph_detects_old_shape():
    assert is_legacy_graph(LEGACY) is True


def test_native_graph_not_legacy():
    native = {"blocks": [{"id": "f", "type": "fact.bos", "params": {}}], "connections": []}
    assert is_legacy_graph(native) is False


def test_compile_legacy_produces_valid_typed_graph():
    typed = compile_legacy(LEGACY)
    # Should validate cleanly against the typed catalog.
    validate_graph(typed)
    types = {b["type"] for b in typed["blocks"]}
    assert "fact.ema_cross" in types
    assert "policy.ema_trend" in types
    assert "interp.confluence_and" in types
    assert "interp.direction_gate" in types
    assert "exec.order_constructor" in types


def test_compile_legacy_uses_collision_safe_synthetic_ids():
    graph = {
        "blocks": [
            {"id": "confluence", "type": "signal.ema_cross", "params": {}},
            {"id": "gate", "type": "entry.both", "params": {}},
            {"id": "order", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "risk", "type": "risk.pct_risk", "params": {}},
        ],
        "connections": [
            {"from": "confluence", "to": "gate"},
            {"from": "gate", "to": "order"},
            {"from": "order", "to": "risk"},
        ],
    }

    typed = compile_legacy(graph)

    ids = [block["id"] for block in typed["blocks"]]
    assert len(ids) == len(set(ids))
    validate_graph(typed)
