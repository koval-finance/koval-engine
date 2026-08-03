"""Per-bar topological executor for a validated typed dataflow graph."""

from __future__ import annotations

from dataclasses import dataclass, field

from koval.strategy.graph.entities import Entity, OrderRequest, TradingIntent
from koval.strategy.graph.node import BarContext, GraphNode
from koval.strategy.graph.registry import NODE_CATALOG
from koval.strategy.graph.state_store import NodeStateStore
from koval.strategy.graph.validation import validate_graph


class GraphExecutionError(RuntimeError):
    """Raised when a node violates its declared runtime contract."""


@dataclass
class StepResult:
    entities_by_node: dict[str, dict[str, Entity | None]] = field(default_factory=dict)
    intents: list[TradingIntent] = field(default_factory=list)
    orders: list[OrderRequest] = field(default_factory=list)


@dataclass
class _Edge:
    src: str
    src_port: str
    dst: str
    dst_port: str


class GraphExecutor:
    def __init__(self, nodes: list[GraphNode], edges: list[_Edge], order: list[str]) -> None:
        self._nodes = {n.node_id: n for n in nodes}
        self._edges = edges
        self._order = order  # node ids in topological order
        self.state = NodeStateStore()
        # incoming edges grouped by destination node id
        self._incoming: dict[str, list[_Edge]] = {nid: [] for nid in self._nodes}
        for e in edges:
            self._incoming[e.dst].append(e)
        # output ports consumed by some edge — used to collect only terminal orders
        self._consumed_ports: set[tuple[str, str]] = {(e.src, e.src_port) for e in edges}

    @classmethod
    def build(cls, graph: dict) -> GraphExecutor:
        validate_graph(graph)
        nodes: list[GraphNode] = []
        for b in graph["blocks"]:
            spec = NODE_CATALOG[b["type"]]
            params = spec.params_schema(**(b.get("params") or {}))
            nodes.append(GraphNode(b["id"], spec, params, spec.factory(params)))
        edges = [
            _Edge(c["from"], c["from_port"], c["to"], c["to_port"])
            for c in (graph.get("connections") or [])
        ]
        order = cls._topo_order([n.node_id for n in nodes], edges)
        return cls(nodes, edges, order)

    @staticmethod
    def _topo_order(ids: list[str], edges: list[_Edge]) -> list[str]:
        adj: dict[str, list[str]] = {i: [] for i in ids}
        indeg = dict.fromkeys(ids, 0)
        for e in edges:
            adj[e.src].append(e.dst)
            indeg[e.dst] += 1
        # deterministic: process ready nodes in sorted id order
        ready = sorted(i for i in ids if indeg[i] == 0)
        out: list[str] = []
        while ready:
            n = ready.pop(0)
            out.append(n)
            for m in sorted(adj[n]):
                indeg[m] -= 1
                if indeg[m] == 0:
                    ready.append(m)
            ready.sort()
        return out

    def step(self, ctx: BarContext) -> StepResult:
        result = StepResult()
        outputs: dict[str, dict[str, Entity | None]] = {}
        for nid in self._order:
            node = self._nodes[nid]
            inputs: dict[str, list[Entity]] = {p: [] for p in node.spec.input_ports}
            for e in self._incoming[nid]:
                produced = outputs.get(e.src, {}).get(e.src_port)
                if produced is not None:
                    inputs[e.dst_port].append(produced)
            node_state = self.state.get(nid)
            produced_map = node.evaluate(ctx, inputs, node_state)
            self._validate_outputs(node, produced_map)
            produced_map = {
                port_name: (
                    entity.model_copy(update={"source_node_id": nid})
                    if entity is not None
                    else None
                )
                for port_name, entity in produced_map.items()
            }
            outputs[nid] = produced_map
            for port_name, ent in produced_map.items():
                if isinstance(ent, TradingIntent):
                    result.intents.append(ent)
                elif (
                    isinstance(ent, OrderRequest)
                    and node.spec.output_ports[port_name].terminal
                    and (nid, port_name) not in self._consumed_ports
                ):
                    # A terminal declaration is an explicit execution boundary.
                    # Unconsumed draft/pricing/sizing outputs never leave the graph.
                    result.orders.append(ent)
        result.entities_by_node = outputs
        return result

    @staticmethod
    def _validate_outputs(node: GraphNode, produced_map: object) -> None:
        if not isinstance(produced_map, dict):
            raise GraphExecutionError(
                f"Node {node.node_id!r} returned {type(produced_map).__name__}, expected an output map"
            )
        undeclared = set(produced_map) - set(node.spec.output_ports)
        if undeclared:
            names = ", ".join(sorted(str(name) for name in undeclared))
            raise GraphExecutionError(
                f"Node {node.node_id!r} returned undeclared output port(s): {names}"
            )
        missing = set(node.spec.output_ports) - set(produced_map)
        if missing:
            names = ", ".join(sorted(missing))
            raise GraphExecutionError(
                f"Node {node.node_id!r} omitted declared output port(s): {names}"
            )
        for port_name, entity in produced_map.items():
            if entity is None:
                continue
            port = node.spec.output_ports[port_name]
            if not isinstance(entity, port.accepts):
                accepted = ", ".join(entity_type.__name__ for entity_type in port.accepts)
                raise GraphExecutionError(
                    f"Node {node.node_id}.{port_name} returned "
                    f"{type(entity).__name__}; expected {accepted}"
                )
