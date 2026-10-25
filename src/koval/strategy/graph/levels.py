"""Public historical-level semantic discovery and same-decision binding oracle."""

from __future__ import annotations

from koval.strategy.graph.entities import LevelSnapshotV1, MarketEvent
from koval.strategy.graph.level_identity import (
    LEVEL_FORMULA_VERSION,
    LEVEL_STATE_VERSION,
    decision_cursor,
    opportunity_id,
)

LEVEL_CAPABILITIES_SCHEMA = "koval_historical_levels_capabilities_v1"
LEVEL_HISTORY_SCHEMA = "koval_level_history_v1"


def level_event_binding(ctx, level: LevelSnapshotV1, event: MarketEvent | None) -> bool:
    """Structural oracle only: a future risk consumer must also admit its own scope."""
    if not isinstance(level, LevelSnapshotV1) or not isinstance(event, MarketEvent):
        return False
    cursor = decision_cursor(ctx)
    data = event.metadata
    return bool(
        level.status == "consumed"
        and level.readiness == "ready"
        and level.symbol == ctx.symbol
        and level.timeframe == ctx.timeframe
        and level.source_node_id == event.source_node_id
        and level.consumption_cursor == cursor == data.get("consumption_cursor")
        and level.event_id == data.get("event_id") == opportunity_id(level.level_id, cursor)
        and data.get("level_id") == level.level_id
        and data.get("trigger_key") == level.trigger_key
        and data.get("formula_version") == level.formula_version
        and level.timestamp_ms == ctx.timestamp_ms
        and level.bar_index == ctx.bar_index
        and level.consumed_at_ms == ctx.decision_timestamp_ms
        and event.price_levels == [level.lower, level.upper]
        and data.get("source_hash") == level.source_hash
        and data.get("lower") == level.lower
        and data.get("upper") == level.upper
        and data.get("center") == level.center
        and event.timestamp_ms == ctx.timestamp_ms
        and event.bar_index == ctx.bar_index
        and data.get("decision_timestamp_ms") == ctx.decision_timestamp_ms
        and event.kind == "level_opportunity"
        and event.direction == "bullish"
    )


def graph_level_history_requirements(graph: dict) -> dict:
    """Compose temporal readiness through levels without rewriting earlier schemas.

    One-based ideal lower bounds assume uninterrupted valid inputs and qualifying
    predicates. Data may postpone arming/success indefinitely. Expiry is a later
    streaming horizon, never a source-buffer or fixed warmup requirement.
    """
    from koval.strategy.graph.condition_history import graph_condition_history_requirements
    from koval.strategy.graph.values import ValueSpecV1

    old = graph_condition_history_requirements(graph)  # also validates the DAG
    blocks = {b["id"]: b for b in graph["blocks"]}
    parents = {nid: [] for nid in blocks}
    incoming_edges = {nid: [] for nid in blocks}
    for c in graph.get("connections") or []:
        parents[c["to"]].append(c["from"])
        incoming_edges[c["to"]].append(c)
    ready, true, prefix, opportunities, expiry, source = {}, {}, {}, {}, {}, {}
    composable = {
        "fact.compare",
        "fact.distance",
        "state.condition_hold",
        "fact.condition_edge",
        "fact.cross",
        "policy.from_condition",
    }

    def visit(nid):
        if nid in ready:
            return
        for parent in parents[nid]:
            visit(parent)
        b, incoming = blocks[nid], parents[nid]
        kind = b["type"]
        if kind == "source.value":
            r = t = replay = ValueSpecV1(**(b.get("params") or {})).required_history_bars()
        elif (
            incoming
            and all(ready[p] is not None for p in incoming)
            and kind in composable | {"state.level_setup"}
        ):

            def available(edge, values):
                parent = edge["from"]
                if blocks[parent]["type"] == "state.level_setup" and edge["from_port"] == "event":
                    return opportunities[parent]
                return values[parent]

            r, t = (
                max(available(edge, values) for edge in incoming_edges[nid])
                for values in (ready, true)
            )
            replay = max(prefix[p] for p in incoming)
            if kind == "state.condition_hold":
                t += b["params"]["bars"] - 1
                replay += b["params"]["bars"] - 1
            elif kind in {"fact.condition_edge", "fact.cross"}:
                r = t = max(r + 1, t)
                replay += 1
            elif kind == "state.level_setup":
                r = t = max(t, r)
                opportunities[nid] = r + 1
                replay += 1
        else:
            r = t = replay = None
        if kind == "state.level_setup":
            opportunities.setdefault(nid, None)
            expiry[nid] = b["params"]["expires_after_bars"]
        ready[nid], true[nid], prefix[nid] = r, t, replay
        source[nid] = (
            r
            if kind == "source.value"
            else max(source[p] for p in incoming)
            if incoming and all(source[p] is not None for p in incoming) and r is not None
            else None
        )

    for nid in blocks:
        visit(nid)
    return {
        "schema": LEVEL_HISTORY_SCHEMA,
        "source_history_bars": old["source_history_bars"],
        "source_history_bars_by_node": source,
        "first_ready_bar_by_node": ready,
        "first_true_bar_by_node": true,
        "first_opportunity_bar_by_node": opportunities,
        "level_expiry_bars_by_node": expiry,
        "replay_prefix_bars": max((p for p in prefix.values() if p is not None), default=0),
        "unknown_nodes": [nid for nid in blocks if ready[nid] is None],
        "assumptions": "ideal uninterrupted valid primary data; triggers/predicates may delay indefinitely",
        "restored_state": "validated post-bar state replaces the consumed temporal prefix; no adapter resume claim",
    }


def level_capabilities() -> dict:
    """Engine semantics only; neither numeric capability nor version proves execution."""
    from koval.strategy.graph.registry import get_node

    spec = get_node("state.level_setup")
    return {
        "schema": LEVEL_CAPABILITIES_SCHEMA,
        "history_schema": LEVEL_HISTORY_SCHEMA,
        "fixture_schema": "koval_historical_levels_fixture_v1",
        "entity_version": 1,
        "formula_version": LEVEL_FORMULA_VERSION,
        "state_version": LEVEL_STATE_VERSION,
        "entity_schema": LevelSnapshotV1.model_json_schema(),
        "parameter_schema": spec.params_schema.model_json_schema(),
        "ports": {
            name: {
                "entity": p.accepts[0].__name__,
                "required": p.required,
                "max_connections": p.max_connections,
            }
            for name, p in spec.input_ports.items()
        },
        "outputs": {
            name: {"entity": p.accepts[0].__name__, "terminal": p.terminal}
            for name, p in spec.output_ports.items()
        },
        "node_type": spec.type,
        "domain": "state",
        "domain_rank": 1,
        "supported_modes": ["sweep_reclaim", "breakout_retest", "breakout"],
        "execution_modes": ["historical"],
        "unsupported_modes": [
            "paper",
            "binance_sandbox",
            "binance_spot_testnet",
            "live",
            "recipes",
            "htf",
            "cross_symbol",
        ],
        "availability_model": "historical_ideal_close_v1",
        "contexts": ["primary"],
        "required_execution_capability": "numeric_values_v1",
        "adapter_execution_verified": False,
        "adapter_resume_supported": False,
        "source_history_limit": 1000,
        "numeric_period_limit": 400,
        "temporal_prefix_limit": None,
        "readiness": "null unavailable snapshot until valid trigger and ready positive causal price bounds",
        "duplicate_policy": "node: identical last cursor/payload gives stable snapshot/no event; graph: preflight immutable primary inputs before upstream nodes, return level snapshots with all other ports null; strategy: immutable cached intent queries preserved; conflicts refuse",
        "graph_binding_version": 1,
        "checkpoint_validation": "strict typed arming evidence, normalized upstream dependency binding and immutable primary input fingerprint",
        "rearm_policy": "new_trigger_after_terminal on a strictly later primary cursor only",
        "dedup_scope": "O(1) last decision payload plus monotone trigger cursor watermark; original legacy event payload hash is its key; stale triggers never arm",
        "invalidation_policy": "expire beyond inclusive deadline; then primary gaps/missingness; then strict open/close below frozen lower buffered threshold; all precede entry",
        "timing": "trigger close arms only; eligible t+1 through t+M inclusive; opportunity consumes before downstream decisions",
        "limitations": [
            "bullish opportunity only",
            "no fill, stop, sizing or detector",
            "no operational admission or pending-order recovery",
            "full software/input identity required for exact replay",
        ],
    }
