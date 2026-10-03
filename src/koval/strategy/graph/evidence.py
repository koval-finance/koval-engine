"""Compact observations made during graph evaluation, never a second evaluator."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any

VERSION = "koval_trade_decision_context_v1"
_SUPPORTED = {
    "fact.rsi_cross",
    "fact.ema_cross",
    "fact.every_bar",
    "policy.ema_trend",
    "policy.macd",
    "policy.rsi",
    "policy.atr_volatility",
    "policy.cooldown",
    "state.trend_bias",
    "state.volatility_regime",
    "interp.confluence_and",
    "interp.direction_gate",
    "interp.event_aggregator",
    "interp.setup_generator",
    "interp.setup_score",
    "interp.context_score",
    "interp.qualification_gate",
    "interp.signal_emitter",
    "exec.order_constructor",
}


def _plain(value):
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "item"):
        if getattr(value, "size", 1) != 1:
            return None
        value = value.item()
    if isinstance(value, float) and not isfinite(value):
        return None
    return value


@dataclass
class NodeEvidence:
    node_id: str
    runtime_node_id: str
    source_node_ids: list[str]
    node_type: str
    name: str
    params: dict
    timeframe: str | None
    timestamp_ms: int
    values: dict = field(default_factory=dict)
    predicate: str | None = None
    result: str = "unavailable"
    role: str = "fact"
    contributing_nodes: list[str] = field(default_factory=list)
    outputs: dict = field(default_factory=dict)


@dataclass
class RiskEvidence:
    basis_name: str
    basis_value: float
    risk_pct: float
    risk_budget: float
    reference_entry: float
    stop_loss: float
    take_profit: float | None
    requested_quantity: float


@dataclass
class DecisionContext:
    signal_bar_open_ms: int
    decision_timestamp_ms: int | None
    history_start_ms: int | None
    history_end_ms: int | None
    history_bars: int
    nodes: list[dict]
    risk: dict | None
    status: str
    missing_reasons: list[str]
    version: str = VERSION


class DecisionRecorder:
    """One step's detached evidence; temporal nodes retain only matched facts."""

    def __init__(self):
        self.nodes: list[dict] = []
        self.current: NodeEvidence | None = None
        self._risk_by_node: dict[str, dict] = {}
        self._observations_by_node: dict[str, list[dict]] = {}
        self._dependencies: list[dict] = []
        self.contributing: list[dict] = []

    def begin(self, node, ctx, inputs):
        self.current = NodeEvidence(
            node.node_id,
            node.node_id,
            node.source_node_ids or [node.node_id],
            node.spec.type,
            node.spec.display_name,
            node.params.model_dump(),
            ctx.timeframe,
            ctx.timestamp_ms,
            role=node.spec.domain.name.lower(),
        )
        self.observe(inputs=[entity for entities in inputs.values() for entity in entities])

    def observe(self, *, values=None, predicate=None, result=None, risk=None, inputs=None):
        if self.current is None:
            return
        if inputs is not None:
            # Nodes that select or ignore connected inputs declare what they used.
            inputs = list(inputs)
            self.current.contributing_nodes = list(dict.fromkeys(e.source_node_id for e in inputs))
            self._dependencies = [
                observation
                for entity in inputs
                for observation in self._observations_by_node.get(entity.source_node_id, [])
            ]
        if values is not None:
            self.current.values.update(_plain(values))
        if predicate is not None:
            self.current.predicate = predicate
        if result is not None:
            self.current.result = result
        if risk is not None:
            self._risk_by_node[self.current.runtime_node_id] = asdict(risk)

    def finish(self, outputs):
        n = self.current
        n.outputs = {k: None if v is None else _plain(v.model_dump()) for k, v in outputs.items()}
        if (
            n.node_type in _SUPPORTED
            and n.result == "unavailable"
            and (n.node_type.startswith(("interp.", "exec.")) or n.node_type == "fact.every_bar")
        ):
            n.result = (
                "not_evaluated" if not any(v is not None for v in outputs.values()) else "recorded"
            )
        if (
            not n.values
            and n.node_type in _SUPPORTED
            and (n.node_type.startswith(("interp.", "exec.")) or n.node_type == "fact.every_bar")
        ):
            fields = {
                "kind",
                "direction",
                "status",
                "allowed",
                "reason",
                "setup_type",
                "window",
                "contributing_events",
                "setup_quality_score",
                "context_score",
                "final_score",
                "qualified",
                "side",
                "quantity",
                "entry_price",
                "stop_price",
                "target_price",
            }
            for output in n.outputs.values():
                if output is not None:
                    n.values.update({key: value for key, value in output.items() if key in fields})
        if n.node_type == "interp.qualification_gate" and "qualified" in n.values:
            n.predicate = "final_score >= qualification_threshold"
            n.result = "passed" if n.values["qualified"] else "failed"
        observation = asdict(n)
        self.nodes.append(observation)
        self._observations_by_node[n.runtime_node_id] = self._unique(
            [*self._dependencies, observation]
        )
        self.current = None

    @staticmethod
    def _unique(observations):
        # A retained fact and its current evaluation are distinct observations.
        return list(
            {
                (n["runtime_node_id"], n["timestamp_ms"]): n for n in observations if n is not None
            }.values()
        )

    def observations(self, entity):
        """Detach an emitted entity's observed dependencies for temporal retention."""
        return deepcopy(self._observations_by_node.get(entity.source_node_id, []))

    def retain(self, observations, source_node_ids):
        """Use only the selected temporal chain, including its original clocks."""
        self._dependencies = deepcopy(observations)
        self.current.contributing_nodes = list(dict.fromkeys(source_node_ids))
        self.contributing.extend(deepcopy(observations))

    def snapshot(self, ctx, order=None):
        if order is None:
            # No trading decision: keep the whole step available for diagnostics.
            nodes = self._unique([*self.nodes, *self.contributing])
            risk = None
        else:
            nodes = self._observations_by_node.get(order.source_node_id, [])
            risk = self._risk_by_node.get(order.source_node_id)
        nodes = deepcopy(nodes)
        missing = [
            f"unsupported_node:{n['runtime_node_id']}"
            for n in nodes
            if n["node_type"] not in _SUPPORTED
        ]
        missing.extend(
            f"values_unavailable:{n['runtime_node_id']}"
            for n in nodes
            if n["result"] == "unavailable" and n["node_type"] in _SUPPORTED
        )
        return _plain(
            asdict(
                DecisionContext(
                    ctx.timestamp_ms,
                    ctx.decision_timestamp_ms,
                    ctx.history_start_ms,
                    ctx.decision_timestamp_ms,
                    0 if ctx.closes is None else len(ctx.closes),
                    nodes,
                    deepcopy(risk),
                    "partial" if missing else "recorded",
                    missing,
                )
            )
        )


def observe(ctx, **kwargs: Any) -> None:
    if ctx.evidence is not None:
        ctx.evidence.observe(**kwargs)
