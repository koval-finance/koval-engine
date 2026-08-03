"""Per-run node-state store. One instance per backtest run; reset between runs.
Holds only plain data so it survives a process-pool job queue."""

from __future__ import annotations

from koval.strategy.graph.node import NodeState


class NodeStateStore:
    def __init__(self) -> None:
        self._states: dict[str, NodeState] = {}

    def get(self, node_id: str) -> NodeState:
        return self._states.setdefault(node_id, {})

    def reset(self) -> None:
        self._states.clear()
