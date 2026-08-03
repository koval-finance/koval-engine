"""Structural + typed validation for native dataflow graphs.

Raises :class:`koval.strategy.block_assembler.GraphValidationError` so legacy
and native graph callers receive the same public validation error.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from koval.strategy.block_assembler import GraphValidationError
from koval.strategy.graph.domains import can_flow
from koval.strategy.graph.ports import port_accepts
from koval.strategy.graph.registry import NODE_CATALOG, ensure_node_catalog


def validate_graph(graph: dict) -> None:
    ensure_node_catalog()
    if not isinstance(graph, dict):
        raise GraphValidationError("Graph must be an object")
    blocks = graph.get("blocks")
    connections = graph.get("connections", [])
    if not isinstance(blocks, list):
        raise GraphValidationError("Graph 'blocks' must be a list")
    if not isinstance(connections, list):
        raise GraphValidationError("Graph 'connections' must be a list")
    if not blocks:
        raise GraphValidationError("Graph has no blocks")

    ids: dict[str, str] = {}  # id -> type
    for b in blocks:
        if not isinstance(b, dict):
            raise GraphValidationError(f"Block must be an object: {b!r}")
        if "id" not in b or "type" not in b:
            raise GraphValidationError(f"Block missing id/type: {b!r}")
        if not isinstance(b["id"], str) or not b["id"]:
            raise GraphValidationError(f"Block id must be a non-empty string: {b!r}")
        if not isinstance(b["type"], str) or not b["type"]:
            raise GraphValidationError(f"Block type must be a non-empty string: {b!r}")
        if b["id"] in ids:
            raise GraphValidationError(f"Duplicate block id {b['id']!r}")
        if b["type"] not in NODE_CATALOG:
            raise GraphValidationError(f"Block {b['id']!r}: unknown type {b['type']!r}")
        ids[b["id"]] = b["type"]
        spec = NODE_CATALOG[b["type"]]
        params = b.get("params") or {}
        if not isinstance(params, dict):
            raise GraphValidationError(f"Block {b['id']!r} params must be an object")
        try:
            spec.params_schema(**params)
        except ValidationError as e:
            raise GraphValidationError(
                f"Block {b['id']!r} ({b['type']}) params invalid: {e}"
            ) from e

    seen_connections: set[tuple[str, str, str, str]] = set()
    incoming_counts: dict[tuple[str, str], int] = {}
    for c in connections:
        if not isinstance(c, dict):
            raise GraphValidationError(f"Connection must be an object: {c!r}")
        src, dst = c.get("from"), c.get("to")
        src_port, dst_port = c.get("from_port"), c.get("to_port")
        if src not in ids or dst not in ids:
            raise GraphValidationError(f"Connection references unknown id: {c!r}")
        src_spec = NODE_CATALOG[ids[src]]
        dst_spec = NODE_CATALOG[ids[dst]]
        if src_port not in src_spec.output_ports:
            raise GraphValidationError(
                f"Connection {c!r}: {ids[src]} has no output port {src_port!r}"
            )
        if dst_port not in dst_spec.input_ports:
            raise GraphValidationError(
                f"Connection {c!r}: {ids[dst]} has no input port {dst_port!r}"
            )
        if not can_flow(src_spec.domain, dst_spec.domain):
            raise GraphValidationError(
                f"Connection {c!r}: domain {src_spec.domain.value} -> "
                f"{dst_spec.domain.value} violates flow rule"
            )
        produced_types = src_spec.output_ports[src_port].accepts
        unsupported = [
            produced
            for produced in produced_types
            if not port_accepts(dst_spec.input_ports[dst_port], produced)
        ]
        if unsupported:
            produced_names = ", ".join(produced.__name__ for produced in unsupported)
            raise GraphValidationError(
                f"Connection {c!r}: type {produced_names} not accepted by {ids[dst]}.{dst_port}"
            )
        edge = (src, src_port, dst, dst_port)
        if edge in seen_connections:
            raise GraphValidationError(f"Duplicate connection: {c!r}")
        seen_connections.add(edge)
        incoming_key = (dst, dst_port)
        incoming_counts[incoming_key] = incoming_counts.get(incoming_key, 0) + 1

    edges = [(c["from"], c["to"]) for c in connections]
    cycle = _find_cycle(set(ids), edges)
    if cycle is not None:
        raise GraphValidationError(f"Graph contains a cycle involving {cycle!r}; need a DAG")

    for node_id, node_type in ids.items():
        spec = NODE_CATALOG[node_type]
        for port_name, port in spec.input_ports.items():
            count = incoming_counts.get((node_id, port_name), 0)
            if port.required and count == 0:
                raise GraphValidationError(f"Unconnected required input port {node_id}.{port_name}")
            if port.max_connections is not None and count > port.max_connections:
                raise GraphValidationError(
                    f"Input port {node_id}.{port_name} accepts at most "
                    f"{port.max_connections} connection(s), got {count}"
                )


def _find_cycle(ids: set[str], edges: list[tuple[str, str]]) -> str | None:
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for src, dst in edges:
        adj[src].append(dst)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(ids, WHITE)
    for start in ids:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, Any]] = [(start, iter(adj[start]))]
        color[start] = GRAY
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = BLACK
                stack.pop()
                continue
            if color[nxt] == GRAY:
                return nxt
            if color[nxt] == WHITE:
                color[nxt] = GRAY
                stack.append((nxt, iter(adj[nxt])))
    return None
