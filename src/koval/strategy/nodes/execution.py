"""EXECUTION compat node. ``exec.order_constructor`` turns a TradingIntent into
an OrderRequest using the existing exit + risk helpers ("simple mode"). The
discrete execution pipeline lives in ``exec_pipeline.py``.

``exec.dynamic_stop`` (trailing / breakeven) is intentionally not implemented as
a node: its faithful typed form needs open-position feedback that the compat
GraphStrategy seam already supplies.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import OrderRequest, TradingIntent
from koval.strategy.graph.node import NodeEvaluate, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.helpers.exits.fixed import fixed_sl_tp
from koval.strategy.helpers.risk.position_sizer import calculate_position_size
from koval.strategy.schemas import _StrictModel


class OrderConstructorParams(_StrictModel):
    # Mirrors FixedSlTpParams + PctRiskParams (+ the entry block's order type) so
    # compat can map entry+exit+risk -> one node.
    sl_pct: float = Field(2.0, gt=0)
    risk_reward: float = Field(2.0, gt=0)
    tp_pct: float | None = Field(None, gt=0)
    risk_pct: float = Field(1.0, gt=0, le=100)
    leverage: float = Field(1.0, gt=0)
    entry_type: Literal["market", "limit", "stop"] = "market"


def _order_constructor_factory(p: OrderConstructorParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        intents = inputs.get("intent", [])
        if not intents:
            return {"order": None}
        intent: TradingIntent = intents[0]
        direction = "long" if intent.side == "buy" else "short"
        entry = float(ctx.close)
        sl, tp = fixed_sl_tp(
            entry_price=entry,
            direction=direction,
            sl_pct=p.sl_pct,
            risk_reward=p.risk_reward,
            tp_pct=p.tp_pct,
        )
        qty = calculate_position_size(
            account_value=float(ctx.account_value),
            risk_per_trade_pct=p.risk_pct,
            entry_price=entry,
            stop_loss=sl,
            direction=direction,
            leverage=p.leverage,
        )
        return {
            "order": OrderRequest(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="order_constructor",
                symbol="",
                side=intent.side,
                entry_price=entry,
                stop_price=sl,
                target_price=tp,
                quantity=qty,
                order_type=p.entry_type,
            )
        }

    return evaluate


def _register() -> None:
    register_node(
        NodeSpec(
            "exec.order_constructor",
            Domain.EXECUTION,
            "Order Constructor",
            "TradingIntent -> OrderRequest (bracket + size from exit/risk helpers).",
            OrderConstructorParams,
            {"intent": PortSpec("intent", (TradingIntent,))},
            {"order": PortSpec("order", (OrderRequest,), terminal=True)},
            _order_constructor_factory,
        )
    )


_register()
