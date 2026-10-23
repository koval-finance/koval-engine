"""Static execution requirements; importing or inspecting a graph performs no I/O."""

from __future__ import annotations


def graph_execution_capabilities(graph: dict) -> frozenset[str]:
    """Derive requirements without evaluating nodes or rewriting saved graphs."""
    required = set()
    for block in graph.get("blocks") or []:
        if block.get("type") in {"exec.spot_dca", "exec.spot_grid"}:
            required.update(
                {
                    "spot_multi_order_v1",
                    "spot_reservations_v1",
                    "spot_causal_ohlcv_v1",
                    "spot_state_restore_v1",
                    block["type"].removeprefix("exec.") + "_v1",
                }
            )
        if block.get("type") == "exec.position_exit":
            required.add("position_exit_v1")
        if (
            block.get("type") == "exec.order_constructor"
            and (block.get("params") or {}).get("take_profit_mode") == "disabled"
        ):
            required.add("optional_take_profit_v1")
    return frozenset(required)
