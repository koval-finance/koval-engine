"""Canonical identities for frozen levels; legacy event serialization is untouched."""

import hashlib
import json

LEVEL_FORMULA_VERSION = "level_setup_v1"
LEVEL_STATE_VERSION = 1


def canonical_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def decision_cursor(ctx) -> str:
    return json.dumps(
        [ctx.bar_index, ctx.timestamp_ms, ctx.decision_timestamp_ms], separators=(",", ":")
    )


def frozen_source_hash(lower, upper) -> str:
    return canonical_hash([lower.model_dump(), None if upper is None else upper.model_dump()])


def frozen_level_id(node_id, trigger_key, source_hash) -> str:
    return canonical_hash([LEVEL_FORMULA_VERSION, node_id, trigger_key, source_hash])


def opportunity_id(level_id, cursor) -> str:
    return canonical_hash(["level_opportunity_v1", level_id, cursor])


def level_graph_binding(graph: dict, node_id: str) -> str:
    """Bind normalized ancestor configuration, excluding moving data windows."""
    from koval.strategy.graph.registry import get_node

    blocks = {b["id"]: b for b in graph["blocks"]}
    incoming = {nid: [] for nid in blocks}
    for edge in graph.get("connections") or []:
        incoming[edge["to"]].append(edge)
    ancestors, edges = set(), []

    def visit(nid):
        if nid in ancestors:
            return
        ancestors.add(nid)
        for edge in incoming[nid]:
            edges.append(edge)
            visit(edge["from"])

    visit(node_id)
    normalized = []
    for nid in sorted(ancestors):
        b = blocks[nid]
        normalized.append(
            {
                "id": nid,
                "type": b["type"],
                "params": get_node(b["type"]).params_schema(**(b.get("params") or {})).model_dump(),
                "source_node_ids": b.get("source_node_ids", [nid]),
            }
        )
    return canonical_hash(
        [
            normalized,
            sorted(edges, key=lambda e: (e["to"], e["to_port"], e["from"], e["from_port"])),
        ]
    )


def primary_context_hash(ctx) -> str:
    """Fingerprint immutable primary decision inputs, not mutable account feedback."""
    from math import isfinite

    import numpy as np

    def plain(value):
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, list):
            return [plain(item) for item in value]
        return value if not isinstance(value, float) or isfinite(value) else repr(value)

    arrays = [
        None
        if getattr(ctx, name) is None
        else plain(np.asarray(getattr(ctx, name), dtype=float).tolist())
        for name in ("opens", "highs", "lows", "closes", "volumes")
    ]
    return canonical_hash(
        [
            ctx.bar_index,
            ctx.timestamp_ms,
            ctx.decision_timestamp_ms,
            ctx.history_start_ms,
            ctx.symbol,
            ctx.timeframe,
            plain([ctx.open, ctx.high, ctx.low, ctx.close, ctx.volume]),
            arrays,
        ]
    )
