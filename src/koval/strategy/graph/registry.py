"""NODE_CATALOG — every typed node keyed by its ``type`` string.

Node modules (``koval/strategy/nodes/*``) register themselves on import; the
package ``koval/strategy/nodes/__init__`` imports them all so importing the
catalog populates it. Mirrors the ``BLOCK_CATALOG`` pattern.
"""

from __future__ import annotations

from koval.strategy.graph.node import NodeSpec

NODE_CATALOG: dict[str, NodeSpec] = {}
_BUILTINS_LOADED = False


def ensure_node_catalog() -> None:
    """Load built-in node modules before public catalog access."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    import koval.strategy.nodes  # noqa: F401

    _BUILTINS_LOADED = True


def register_node(spec: NodeSpec) -> None:
    if spec.type in NODE_CATALOG:
        raise ValueError(f"Node {spec.type!r} already registered")
    NODE_CATALOG[spec.type] = spec


def get_node(node_type: str) -> NodeSpec:
    ensure_node_catalog()
    if node_type not in NODE_CATALOG:
        raise KeyError(node_type)
    return NODE_CATALOG[node_type]


def get_all_nodes() -> list[NodeSpec]:
    ensure_node_catalog()
    return list(NODE_CATALOG.values())


ensure_node_catalog()
