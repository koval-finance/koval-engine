"""Compile a legacy linear-AND BlockGraph into a native typed dataflow graph.

Mapping:
  signal.X  -> fact.X            (-> interp.confluence_and.events)
  filter.X  -> policy.X          (-> interp.direction_gate.policies)
  entry.*   -> interp.direction_gate params (allow_long/allow_short)
  exit.* + risk.* -> exec.order_constructor (merged params)

Legacy connections are validated as an explicit linear trading pipeline before
the typed graph is constructed. Dynamic exits remain attached through the
GraphStrategy lifecycle hook because they do not emit per-bar graph entities.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from koval.strategy.block_assembler import GraphValidationError
from koval.strategy.registry import BLOCK_CATALOG  # legacy catalog

_ENTRY_DIRECTIONS = {
    "entry.long_only": (True, False),
    "entry.short_only": (False, True),
    "entry.both": (True, True),
}


def is_legacy_graph(graph: dict) -> bool:
    if not isinstance(graph, dict):
        return False
    blocks = graph.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return False
    return all(isinstance(b, dict) and b.get("type") in BLOCK_CATALOG for b in blocks)


_ALLOWED_FLOWS = {
    "signal": {"filter", "entry"},
    "filter": {"filter", "entry"},
    "entry": {"exit"},
    "exit": {"dynamic_exit", "risk"},
    "dynamic_exit": {"risk"},
    "risk": set(),
}


def _validate_legacy_graph(graph: dict) -> dict[str, list[dict]]:
    blocks = graph.get("blocks")
    connections = graph.get("connections")
    if not isinstance(blocks, list) or not blocks:
        raise GraphValidationError("Legacy graph has no blocks")
    if not isinstance(connections, list):
        raise GraphValidationError("Legacy graph 'connections' must be a list")

    by_id: dict[str, dict] = {}
    by_cat: dict[str, list[dict]] = {}
    for block in blocks:
        if not isinstance(block, dict):
            raise GraphValidationError(f"Block must be an object: {block!r}")
        block_id = block.get("id")
        block_type = block.get("type")
        if not isinstance(block_id, str) or not block_id:
            raise GraphValidationError(f"Block id must be a non-empty string: {block!r}")
        if block_id in by_id:
            raise GraphValidationError(f"Duplicate block id {block_id!r}")
        spec = BLOCK_CATALOG.get(block_type)
        if spec is None:
            raise GraphValidationError(f"Block {block_id!r}: unknown type {block_type!r}")
        params = block.get("params") or {}
        if not isinstance(params, dict):
            raise GraphValidationError(f"Block {block_id!r} params must be an object")
        try:
            spec.params_schema(**params)
        except ValidationError as exc:
            raise GraphValidationError(
                f"Block {block_id!r} ({block_type}) params invalid: {exc}"
            ) from exc
        by_id[block_id] = block
        by_cat.setdefault(spec.category, []).append(block)

    _require_cardinality(by_cat, "signal", minimum=1)
    _require_cardinality(by_cat, "entry", exact=1)
    _require_cardinality(by_cat, "exit", exact=1)
    _require_cardinality(by_cat, "risk", exact=1)
    if len(by_cat.get("dynamic_exit", [])) > 1:
        raise GraphValidationError("Legacy graph accepts at most one dynamic_exit block")

    adjacency: dict[str, set[str]] = {block_id: set() for block_id in by_id}
    seen: set[tuple[str, str]] = set()
    for connection in connections:
        if not isinstance(connection, dict):
            raise GraphValidationError(f"Connection must be an object: {connection!r}")
        src, dst = connection.get("from"), connection.get("to")
        if src not in by_id or dst not in by_id:
            raise GraphValidationError(f"Connection references unknown id: {connection!r}")
        edge = (src, dst)
        if edge in seen:
            raise GraphValidationError(f"Duplicate connection: {connection!r}")
        seen.add(edge)
        src_category = BLOCK_CATALOG[by_id[src]["type"]].category
        dst_category = BLOCK_CATALOG[by_id[dst]["type"]].category
        if dst_category not in _ALLOWED_FLOWS[src_category]:
            raise GraphValidationError(
                f"Connection {src!r} -> {dst!r} violates legacy flow "
                f"{src_category} -> {dst_category}"
            )
        adjacency[src].add(dst)

    entry_id = by_cat["entry"][0]["id"]
    exit_id = by_cat["exit"][0]["id"]
    risk_id = by_cat["risk"][0]["id"]
    for block in by_cat["signal"] + by_cat.get("filter", []):
        if not _can_reach(block["id"], entry_id, adjacency):
            raise GraphValidationError(
                f"Block {block['id']!r} does not connect to entry block {entry_id!r}"
            )
    if not _can_reach(entry_id, exit_id, adjacency):
        raise GraphValidationError(
            f"Entry block {entry_id!r} does not connect to exit block {exit_id!r}"
        )
    if not _can_reach(exit_id, risk_id, adjacency):
        raise GraphValidationError(
            f"Exit block {exit_id!r} does not connect to risk block {risk_id!r}"
        )
    for block in by_cat.get("dynamic_exit", []):
        if not (
            _can_reach(exit_id, block["id"], adjacency)
            and _can_reach(block["id"], risk_id, adjacency)
        ):
            raise GraphValidationError(
                f"Dynamic exit block {block['id']!r} must connect the exit and risk blocks"
            )
    return by_cat


def _require_cardinality(
    by_cat: dict[str, list[dict]],
    category: str,
    *,
    minimum: int | None = None,
    exact: int | None = None,
) -> None:
    count = len(by_cat.get(category, []))
    if exact is not None and count != exact:
        raise GraphValidationError(
            f"Legacy graph requires exactly one {category} block; found {count}"
        )
    if minimum is not None and count < minimum:
        raise GraphValidationError(
            f"Legacy graph requires at least {minimum} {category} block; found {count}"
        )


def _can_reach(start: str, target: str, adjacency: dict[str, set[str]]) -> bool:
    pending = [start]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(adjacency[current] - seen)
    return False


def _synthetic_id(label: str, used_ids: set[str]) -> str:
    candidate = f"__koval_{label}"
    suffix = 2
    while candidate in used_ids:
        candidate = f"__koval_{label}_{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def compile_legacy(graph: dict) -> dict:
    by_cat = _validate_legacy_graph(graph)
    used_ids = {block["id"] for block in graph["blocks"]}
    confluence_id = _synthetic_id("confluence", used_ids)
    gate_id = _synthetic_id("gate", used_ids)
    order_id = _synthetic_id("order", used_ids)

    typed_blocks: list[dict] = []
    connections: list[dict] = []

    # signals -> fact.* -> confluence
    signals = by_cat.get("signal", [])
    for b in signals:
        node_type = "fact." + b["type"].split(".", 1)[1]
        typed_blocks.append({"id": b["id"], "type": node_type, "params": b.get("params", {})})
        connections.append(
            {
                "from": b["id"],
                "from_port": "event",
                "to": confluence_id,
                "to_port": "events",
            }
        )
    # Legacy linear-AND required every wired signal to fire and agree; min_signals
    # pins that (a non-firing signal emits no event).
    typed_blocks.append(
        {
            "id": confluence_id,
            "type": "interp.confluence_and",
            "params": {"min_signals": len(signals)},
        }
    )

    # entry -> direction_gate params (+ entry_type carried into the order)
    entry = by_cat["entry"][0]
    allow_long, allow_short = _ENTRY_DIRECTIONS[entry["type"]]
    entry_type = (entry.get("params") or {}).get("entry_type", "market")
    typed_blocks.append(
        {
            "id": gate_id,
            "type": "interp.direction_gate",
            "params": {"allow_long": allow_long, "allow_short": allow_short},
        }
    )
    connections.append(
        {
            "from": confluence_id,
            "from_port": "agreement",
            "to": gate_id,
            "to_port": "agreement",
        }
    )

    # filters -> policy.* -> gate.policies
    for b in by_cat.get("filter", []):
        node_type = "policy." + b["type"].split(".", 1)[1]
        typed_blocks.append({"id": b["id"], "type": node_type, "params": b.get("params", {})})
        connections.append(
            {
                "from": b["id"],
                "from_port": "policy",
                "to": gate_id,
                "to_port": "policies",
            }
        )

    # entry_type + exit + risk -> exec.order_constructor
    exit_p = by_cat["exit"][0].get("params") or {}
    risk_p = by_cat["risk"][0].get("params") or {}
    typed_blocks.append(
        {
            "id": order_id,
            "type": "exec.order_constructor",
            "params": {**exit_p, **risk_p, "entry_type": entry_type},
        }
    )
    connections.append(
        {
            "from": gate_id,
            "from_port": "intent",
            "to": order_id,
            "to_port": "intent",
        }
    )

    return {"blocks": typed_blocks, "connections": connections}


def extract_dynamic_exit(graph: dict) -> Callable | None:
    """Bind the legacy ``dynamic_exit`` block's callable, if the graph has one.

    Carried on the GraphStrategy seam so trailing / breakeven keep working
    through the compat path; a native ``exec.dynamic_stop`` node does not exist.
    Returns the first dynamic-exit callable, or ``None``.
    """
    for b in graph.get("blocks") or []:
        spec = BLOCK_CATALOG.get(b["type"])
        if spec is not None and spec.category == "dynamic_exit":
            params = spec.params_schema(**(b.get("params") or {}))
            return spec.factory(params)
    return None
