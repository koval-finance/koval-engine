"""Static historical-condition discovery; no plugin imports or execution claims."""

from __future__ import annotations

from koval.strategy.graph.values import VALUE_CAPABILITY, VALUE_FORMULA_VERSION, ValueSpecV1

CONDITION_HISTORY_SCHEMA = "koval_condition_history_v1"
CONDITION_CAPABILITIES_SCHEMA = "koval_historical_conditions_capabilities_v1"
MAX_CONDITION_SOURCE_HISTORY_BARS = 1000


def graph_condition_history_requirements(graph: dict) -> dict:
    """Separate physical source rows from a streamed, uninterrupted replay prefix.

    Bar numbers are one-based. Bounds assume valid data and true conditions;
    runtime missingness can delay them indefinitely. Unknown/legacy producer
    requirements are null, never guessed. They must be budgeted by their host.
    The conservative edge prefix allows one prior observation after its upstream
    true-prefix bound; the earliest event can precede that allowance by one bar.
    """
    from koval.strategy.graph.validation import validate_graph

    validate_graph(graph)
    blocks = {block["id"]: block for block in graph["blocks"]}
    incoming = {nid: [] for nid in blocks}
    for connection in graph.get("connections") or []:
        incoming[connection["to"]].append(connection["from"])
    ready, true, prefix = {}, {}, {}

    def visit(nid):
        if nid in ready:
            return
        for parent in incoming[nid]:
            visit(parent)
        block = blocks[nid]
        kind = block["type"]
        parents = incoming[nid]
        if kind == "source.value":
            h = ValueSpecV1(**(block.get("params") or {})).required_history_bars()
            ready[nid] = true[nid] = prefix[nid] = h
            return
        known = parents and all(ready[p] is not None for p in parents)
        if not known or kind not in {
            "fact.compare",
            "fact.distance",
            "state.condition_hold",
            "fact.condition_edge",
            "fact.cross",
            "policy.from_condition",
        }:
            ready[nid] = true[nid] = prefix[nid] = None
            return
        r = max(ready[p] for p in parents)
        t = max(true[p] for p in parents)
        replay = max(prefix[p] for p in parents)
        if kind == "state.condition_hold":
            latency = block["params"]["bars"] - 1
            t += latency
            replay += latency
        elif kind in {"fact.condition_edge", "fact.cross"}:
            r = t = max(r + 1, t)
            replay += 1
        ready[nid], true[nid], prefix[nid] = r, t, replay

    for nid in blocks:
        visit(nid)
    return {
        "schema": CONDITION_HISTORY_SCHEMA,
        "source_history_bars": max(
            (
                ValueSpecV1(**(b.get("params") or {})).required_history_bars()
                for b in blocks.values()
                if b["type"] == "source.value"
            ),
            default=0,
        ),
        "first_ready_bar_by_node": ready,
        "first_true_bar_by_node": true,
        "replay_prefix_bars": max((p for p in prefix.values() if p is not None), default=0),
        "unknown_nodes": [nid for nid in blocks if ready[nid] is None],
        "assumptions": "uninterrupted valid primary prefix; runtime missingness may delay indefinitely",
        "restored_state": "valid post-bar reducer state replaces its already-consumed temporal prefix",
    }


def condition_capabilities() -> dict:
    """Engine semantics/schema discovery, independent of plugin support/admission.

    Unchanged numeric_values_v1 negotiation is intentional: these are generic
    graph evaluator additions, not new matching or broker operations. A host must
    check exact engine semantics and verify its plugin/data path independently.
    """
    from koval.strategy.graph.registry import get_node
    from koval.strategy.nodes.condition_reducers import (
        EDGE_FORMULA_VERSION,
        EDGE_STATE_VERSION,
        HOLD_FORMULA_VERSION,
        HOLD_STATE_VERSION,
    )

    kinds = ("source.value", "state.condition_hold", "fact.condition_edge")
    return {
        "schema": CONDITION_CAPABILITIES_SCHEMA,
        "history_schema": CONDITION_HISTORY_SCHEMA,
        "required_execution_capability": VALUE_CAPABILITY,
        "availability_model": "historical_ideal_close_v1",
        "modes": ["historical"],
        "unsupported_modes": ["paper", "binance_sandbox", "binance_spot_testnet", "live"],
        "contexts": ["primary"],
        "source_history_limit": MAX_CONDITION_SOURCE_HISTORY_BARS,
        "temporal_prefix_limit": None,
        "adapter_execution_verified": False,
        "adapter_resume_supported": False,
        "formula_versions": {
            "legacy_values": VALUE_FORMULA_VERSION,
            "adx": "adx_wilder_v1",
            "relative_volume": "relative_volume_v1",
            "state.condition_hold": HOLD_FORMULA_VERSION,
            "fact.condition_edge": EDGE_FORMULA_VERSION,
        },
        "state_versions": {
            "state.condition_hold": HOLD_STATE_VERSION,
            "fact.condition_edge": EDGE_STATE_VERSION,
        },
        "parameter_schemas": {
            kind: get_node(kind).params_schema.model_json_schema() for kind in kinds
        },
        "event": {"kind": "condition_edge", "direction": "bullish", "price_levels": []},
        "duplicate_policy": "same binding: prior hold output/no edge; changed binding: refuse",
    }
