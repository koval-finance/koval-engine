"""Single-component primary historical numeric source."""

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import NumericValueV1
from koval.strategy.graph.node import NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.graph.values import ValueSpecV1, value_at
from koval.strategy.nodes.value_contracts import observation


def _source_factory(spec):
    def evaluate(ctx, inputs, state):
        result = value_at(ctx, spec)
        observation(ctx, result, f"confirmed historical ideal close: {spec.component}")
        return {"value": result}

    return evaluate


register_node(
    NodeSpec(
        "source.value",
        Domain.SOURCE,
        "Numeric Value",
        "One confirmed primary historical OHLCV, EMA, ATR, ADX, RVOL, BB or channel component.",
        ValueSpecV1,
        {},
        {"value": PortSpec("value", (NumericValueV1,))},
        _source_factory,
    )
)
