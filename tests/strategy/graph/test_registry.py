import subprocess
import sys

import numpy as np
from pydantic import BaseModel

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import MarketEvent
from koval.strategy.graph.node import BarContext, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import (
    NODE_CATALOG,
    get_node,
    register_node,
)


class _NoParams(BaseModel):
    pass


def _factory(_p):
    def evaluate(ctx, inputs, state):
        return {
            "event": MarketEvent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="x",
                kind="test",
            )
        }

    return evaluate


def test_register_and_get_node():
    spec = NodeSpec(
        type="fact.unit_test_only",
        domain=Domain.FACT,
        display_name="t",
        description="d",
        params_schema=_NoParams,
        input_ports={},
        output_ports={"event": PortSpec("event", (MarketEvent,))},
        factory=_factory,
    )
    register_node(spec)
    assert get_node("fact.unit_test_only") is spec
    del NODE_CATALOG["fact.unit_test_only"]  # keep catalog clean for other tests


def test_bar_context_holds_arrays():
    ctx = BarContext(
        close=10.0,
        high=11.0,
        low=9.0,
        open=9.5,
        volume=100.0,
        bar_index=5,
        timestamp_ms=1000,
        closes=np.array([9.0, 10.0]),
        highs=np.array([10.0, 11.0]),
        lows=np.array([8.0, 9.0]),
        opens=np.array([8.5, 9.5]),
        volumes=np.array([50.0, 100.0]),
        account_value=10_000.0,
    )
    assert ctx.close == 10.0
    assert ctx.account_value == 10_000.0


def test_public_registry_access_populates_builtin_nodes_in_fresh_process():
    code = (
        "from koval.strategy.graph.registry import get_all_nodes; "
        "print(','.join(sorted(spec.type for spec in get_all_nodes())))"
    )
    output = subprocess.check_output([sys.executable, "-c", code], text=True)

    assert "fact.bos" in output.split(",")
    assert "exec.execution_router" in output.split(",")


def test_importing_public_catalog_populates_builtin_nodes_in_fresh_process():
    code = (
        "from koval.strategy.graph.registry import NODE_CATALOG; "
        "print(','.join(sorted(NODE_CATALOG)))"
    )
    output = subprocess.check_output([sys.executable, "-c", code], text=True)

    assert "fact.bos" in output.split(",")
    assert "exec.execution_router" in output.split(",")
