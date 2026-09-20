"""Node runtime contract: BarContext (per-bar inputs), NodeState (cross-bar
mutable state), and the NodeSpec catalog entry with its factory."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
from pydantic import BaseModel

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import Entity
from koval.strategy.graph.ports import PortSpec

if TYPE_CHECKING:
    from koval.engine.account_state import AccountSnapshot
    from koval.strategy.graph.indicators import IndicatorValues


@dataclass
class BarContext:
    """Bar-local inputs, mirroring the fields the BT adapter injects today.

    Arrays are chronological; ``arr[-1]`` is the current (closed) bar.
    """

    close: float
    high: float
    low: float
    open: float
    volume: float
    bar_index: int
    timestamp_ms: int
    closes: np.ndarray | None = None
    highs: np.ndarray | None = None
    lows: np.ndarray | None = None
    opens: np.ndarray | None = None
    volumes: np.ndarray | None = None
    htf_closes: np.ndarray | None = None
    htf_highs: np.ndarray | None = None
    htf_lows: np.ndarray | None = None
    htf_opens: np.ndarray | None = None
    htf_volumes: np.ndarray | None = None
    account_value: float = 0.0
    position_size: float = 0.0
    position_direction: str | None = None
    symbol: str = ""
    account: AccountSnapshot | None = None
    indicators: IndicatorValues | None = None


# Cross-bar mutable state for one node. Plain dict so it stays picklable.
NodeState = dict[str, Any]

# A bound node: reads bar context + upstream entities + its own state,
# returns {output_port_name: Entity | None}.
NodeEvaluate = Callable[
    [BarContext, "dict[str, list[Entity]]", NodeState],
    "dict[str, Entity | None]",
]


@dataclass(frozen=True)
class NodeSpec:
    type: str
    domain: Domain
    display_name: str
    description: str
    params_schema: type[BaseModel]
    input_ports: dict[str, PortSpec]
    output_ports: dict[str, PortSpec]
    factory: Callable[[BaseModel], NodeEvaluate]


@dataclass
class GraphNode:
    """A node instance inside an assembled graph."""

    node_id: str
    spec: NodeSpec
    params: BaseModel
    evaluate: NodeEvaluate = field(repr=False)
