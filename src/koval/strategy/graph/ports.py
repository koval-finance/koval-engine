"""Typed ports. An input port lists accepted entity types; an output port lists
the single type it produces (kept as a 1-tuple for symmetry)."""

from __future__ import annotations

from dataclasses import dataclass

from koval.strategy.graph.entities import Entity


@dataclass(frozen=True)
class PortSpec:
    name: str
    accepts: tuple[type[Entity], ...]
    required: bool = True
    # Input ports accept one incoming edge unless they explicitly declare
    # fan-in with ``None``. Ignored for output ports.
    max_connections: int | None = 1
    # Only an unconsumed OrderRequest on a terminal output may leave the graph.
    terminal: bool = False


def port_accepts(port: PortSpec, produced: type[Entity]) -> bool:
    return any(issubclass(produced, t) for t in port.accepts)
