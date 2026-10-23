"""Native recipe terminals with typed activation, stop and confirmed-state ports."""

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import PolicyDecision, SpotRecipeRequest, SpotRecipeStatus
from koval.strategy.graph.node import NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.schemas import SpotDcaParams, SpotGridParams


def _factory(kind):
    def bind(params):
        def evaluate(ctx, inputs, state):
            view = ctx.spot_runtime
            if view is None:
                raise ValueError("Spot recipe requires a capable multi-order host binding")

            def allowed(name):
                values = inputs.get(name, [])
                return bool(values and values[0].allowed)

            common = {
                "bar_index": ctx.bar_index,
                "timestamp_ms": ctx.timestamp_ms,
                "source_node_id": "recipe",
            }
            basket = view["basket"]
            return {
                "recipe": SpotRecipeRequest(
                    **common,
                    kind=kind,
                    params=params.model_dump(mode="json"),
                    start=allowed("start"),
                    stop_hold=allowed("stop_hold"),
                    stop_flatten=allowed("stop_flatten"),
                ),
                "status": SpotRecipeStatus(
                    **common,
                    kind=kind,
                    phase=basket["phase"],
                    cycle_id=f"c{basket['cycle']}",
                    anchor=basket["anchor"],
                    stop=basket["stop"],
                    target=basket["desired_target"],
                    net_inventory=view["base_quantity"],
                    free_quote=view["free_quote"],
                    reserved_quote=view["quote_reserved"],
                    free_base=view["free_base"],
                    reserved_base=view["base_reserved"],
                    observation_id=str(view.get("observation_id", "initial")),
                    containment_reason=basket["refusal_reason"],
                ),
            }

        return evaluate

    return bind


for _kind, _schema in (("dca", SpotDcaParams), ("grid", SpotGridParams)):
    register_node(
        NodeSpec(
            f"exec.spot_{_kind}",
            Domain.EXECUTION,
            f"Spot {_kind.upper()}",
            "Bounded cash-funded Spot recipe; requires the versioned multi-order execution lane.",
            _schema,
            {
                "start": PortSpec("start", (PolicyDecision,)),
                "stop_hold": PortSpec("stop_hold", (PolicyDecision,), required=False),
                "stop_flatten": PortSpec("stop_flatten", (PolicyDecision,), required=False),
            },
            {
                "recipe": PortSpec("recipe", (SpotRecipeRequest,), terminal=True),
                "status": PortSpec("status", (SpotRecipeStatus,)),
            },
            _factory(_kind),
        )
    )
