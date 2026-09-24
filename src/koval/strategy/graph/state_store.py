"""Per-run node-state store. One instance per backtest run; reset between runs.
Holds only plain data so it survives a process-pool job queue."""

from __future__ import annotations

from copy import deepcopy

from koval.strategy.graph.node import NodeState


class NodeStateStore:
    def __init__(self) -> None:
        self._states: dict[str, NodeState] = {}

    def get(self, node_id: str) -> NodeState:
        return self._states.setdefault(node_id, {})

    def reset(self) -> None:
        self._states.clear()

    def checkpoint(self) -> dict[str, NodeState]:
        """Return an independent plain-data snapshot of every node state."""
        return deepcopy(self._states)

    def restore(self, checkpoint: dict[str, NodeState]) -> None:
        """Replace state from an already integrity-checked runtime checkpoint."""
        if not isinstance(checkpoint, dict) or any(
            not isinstance(node_id, str) or not isinstance(state, dict)
            for node_id, state in checkpoint.items()
        ):
            raise ValueError("node-state checkpoint must map node ids to state objects")
        self._states = deepcopy(checkpoint)
