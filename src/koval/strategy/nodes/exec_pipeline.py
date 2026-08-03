"""Discrete execution pipeline: the EXECUTION nodes that replace the merged
``exec.order_constructor`` for native typed graphs.

    order_pricing -> position_sizing -> trade_risk_gate
                                      \\-> portfolio_risk_gate -> execution_router

Intermediate stages pass a progressively-refined OrderRequest; the two gates emit
PolicyDecision; only the router's order port is terminal (executor collects it).
``exec.order_constructor`` (execution.py) stays as compat "simple mode".
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import (
    MarketEvent,
    MarketState,
    OrderRequest,
    PolicyDecision,
    TradingIntent,
)
from koval.strategy.graph.node import NodeEvaluate, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.helpers._math import _atr
from koval.strategy.helpers.exits.fixed import fixed_sl_tp
from koval.strategy.helpers.exits.pricing import (
    liquidity_target,
    structural_sl,
    volatility_sl,
    zone_entry,
)
from koval.strategy.helpers.risk.gates import portfolio_risk_check, trade_risk_check
from koval.strategy.helpers.risk.position_sizer import calculate_position_size
from koval.strategy.helpers.risk.sizing import required_margin
from koval.strategy.schemas import _StrictModel

# ---------------------------------------------------------------------------
# Order Pricing Constructor
# ---------------------------------------------------------------------------


class OrderPricingParams(_StrictModel):
    entry_model: Literal["market", "limit_at_zone", "stop_market"] = "market"
    sl_model: Literal["fixed_percent", "structural", "volatility"] = "fixed_percent"
    target_model: Literal["fixed_rr", "liquidity_target"] = "fixed_rr"
    sl_pct: float = Field(2.0, gt=0)
    risk_reward: float = Field(2.0, gt=0)
    atr_period: int = Field(14, ge=2)
    atr_mult: float = Field(1.5, gt=0)
    zone_offset_pct: float = Field(0.0, ge=0)


def _geometry_levels(inputs) -> list[float]:
    levels: list[float] = []
    for g in inputs.get("geometry", []):
        levels.extend(g.price_levels)
    return levels


def _order_pricing_factory(p: OrderPricingParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        intents = inputs.get("intent", [])
        if not intents:
            return {"order": None}
        intent: TradingIntent = intents[0]
        direction = "long" if intent.side == "buy" else "short"
        levels = _geometry_levels(inputs)

        # entry
        entry = float(ctx.close)
        if p.entry_model == "limit_at_zone":
            z = zone_entry(price_levels=levels, direction=direction, offset_pct=p.zone_offset_pct)
            if z is not None:
                entry = z

        # base fixed sl/tp (always available as fallback)
        sl, tp = fixed_sl_tp(
            entry_price=entry, direction=direction, sl_pct=p.sl_pct, risk_reward=p.risk_reward
        )

        # stop model override
        if p.sl_model == "structural":
            s = structural_sl(price_levels=levels, direction=direction, entry=entry)
            if s is not None:
                sl = s
        elif p.sl_model == "volatility" and ctx.highs is not None:
            atr_val = _atr(ctx.highs, ctx.lows, ctx.closes, p.atr_period)
            # _atr returns 0.0 on insufficient history; keep the fixed fallback
            # then rather than emit a zero-distance stop (sl == entry).
            if atr_val > 0:
                sl = volatility_sl(
                    entry=entry, atr=float(atr_val), mult=p.atr_mult, direction=direction
                )

        # target model override
        if p.target_model == "liquidity_target":
            t = liquidity_target(price_levels=levels, direction=direction, entry=entry)
            if t is not None:
                tp = t

        order_type = {
            "market": "market",
            "limit_at_zone": "limit",
            "stop_market": "stop",
        }[p.entry_model]
        return {
            "order": OrderRequest(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="order_pricing",
                symbol=ctx.symbol,
                side=intent.side,
                entry_price=entry,
                stop_price=sl,
                target_price=tp,
                quantity=0.0,
                order_type=order_type,
                metadata={
                    "entry_model": p.entry_model,
                    "sl_model": p.sl_model,
                    "target_model": p.target_model,
                },
            )
        }

    return evaluate


# ---------------------------------------------------------------------------
# Position Sizing Engine
# ---------------------------------------------------------------------------


class PositionSizingParams(_StrictModel):
    risk_pct: float = Field(1.0, gt=0, le=100)
    leverage: float = Field(1.0, gt=0)
    instrument_type: Literal["spot", "futures"] = "futures"
    sizing_model: Literal["fixed_risk", "fixed_notional"] = "fixed_risk"
    notional: float | None = Field(None, gt=0)


def _position_sizing_factory(p: PositionSizingParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        orders = inputs.get("order", [])
        if not orders:
            return {"order": None}
        o: OrderRequest = orders[0]
        direction = "long" if o.side == "buy" else "short"
        if p.sizing_model == "fixed_notional" and p.notional:
            qty = p.notional / o.entry_price if o.entry_price > 0 else 0.0
        else:
            qty = calculate_position_size(
                account_value=float(ctx.account_value),
                risk_per_trade_pct=p.risk_pct,
                entry_price=o.entry_price,
                stop_loss=o.stop_price,
                direction=direction,
                leverage=p.leverage,
            )
        margin = required_margin(
            quantity=qty,
            entry_price=o.entry_price,
            leverage=p.leverage,
            instrument_type=p.instrument_type,
        )
        risk_amount = float(ctx.account_value) * (p.risk_pct / 100.0)
        meta: dict[str, Any] = dict(o.metadata)
        meta.update(
            {
                "required_margin": margin,
                "risk_amount": risk_amount,
                "leverage": p.leverage,
                "instrument_type": p.instrument_type,
            }
        )
        return {"order": o.model_copy(update={"quantity": qty, "metadata": meta})}

    return evaluate


# ---------------------------------------------------------------------------
# Trade Risk Gate
# ---------------------------------------------------------------------------


class TradeRiskGateParams(_StrictModel):
    min_rr: float = Field(1.5, gt=0)
    max_stop_distance_pct: float = Field(10.0, gt=0)
    max_leverage: float = Field(20.0, gt=0)


def _trade_risk_gate_factory(p: TradeRiskGateParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        orders = inputs.get("order", [])
        if not orders:
            return {"decision": None}
        o: OrderRequest = orders[0]
        allowed, reason = trade_risk_check(
            entry=o.entry_price,
            stop=o.stop_price,
            target=o.target_price,
            leverage=float(o.metadata.get("leverage", 1.0)),
            min_rr=p.min_rr,
            max_stop_distance_pct=p.max_stop_distance_pct,
            max_leverage=p.max_leverage,
        )
        return {
            "decision": PolicyDecision(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="trade_risk_gate",
                allowed=allowed,
                reason=reason,
                scope="execution",
            )
        }

    return evaluate


# ---------------------------------------------------------------------------
# Portfolio Risk Gate
# ---------------------------------------------------------------------------


class PortfolioRiskGateParams(_StrictModel):
    max_daily_drawdown_pct: float = Field(5.0, gt=0)
    max_concurrent_positions: int = Field(1, ge=1)
    max_total_margin_pct: float = Field(50.0, gt=0)


def _portfolio_risk_gate_factory(p: PortfolioRiskGateParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        orders = inputs.get("order", [])
        if not orders:
            return {"decision": None}
        o: OrderRequest = orders[0]
        if ctx.account is None:
            allowed, reason = True, None
        else:
            allowed, reason = portfolio_risk_check(
                account=ctx.account,
                required_margin=float(o.metadata.get("required_margin", 0.0)),
                max_daily_drawdown_pct=p.max_daily_drawdown_pct,
                max_concurrent_positions=p.max_concurrent_positions,
                max_total_margin_pct=p.max_total_margin_pct,
            )
        return {
            "decision": PolicyDecision(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="portfolio_risk_gate",
                allowed=allowed,
                reason=reason,
                scope="execution",
            )
        }

    return evaluate


# ---------------------------------------------------------------------------
# Execution Router
# ---------------------------------------------------------------------------


# Literal-typed target enforces the paper/sandbox hard constraint at schema
# validation: any real-money value fails pydantic with a ValueError.
class ExecutionRouterParams(_StrictModel):
    pass


def _execution_router_factory(p: ExecutionRouterParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        orders = inputs.get("order", [])
        approvals = inputs.get("approvals", [])
        if not orders:
            return {"order": None}
        # Safety chokepoint: an unwired/empty approvals port means no risk gate
        # vouched for this order, so refuse to route it (all([]) would be True).
        if not approvals or not all(d.allowed for d in approvals):
            return {"order": None}
        o: OrderRequest = orders[0]
        return {"order": o.model_copy(update={"source_node_id": "execution_router"})}

    return evaluate


def _register() -> None:
    register_node(
        NodeSpec(
            "exec.order_pricing",
            Domain.EXECUTION,
            "Order Pricing",
            "TradingIntent (+ optional FACT/STATE geometry) -> priced OrderRequest (qty=0).",
            OrderPricingParams,
            {
                "intent": PortSpec("intent", (TradingIntent,)),
                "geometry": PortSpec(
                    "geometry",
                    (MarketEvent, MarketState),
                    required=False,
                    max_connections=None,
                ),
            },
            {"order": PortSpec("order", (OrderRequest,))},
            _order_pricing_factory,
        )
    )
    register_node(
        NodeSpec(
            "exec.position_sizing",
            Domain.EXECUTION,
            "Position Sizing",
            "Priced OrderRequest + account -> sized OrderRequest (qty + margin metadata).",
            PositionSizingParams,
            {"order": PortSpec("order", (OrderRequest,))},
            {"order": PortSpec("order", (OrderRequest,))},
            _position_sizing_factory,
        )
    )
    register_node(
        NodeSpec(
            "exec.trade_risk_gate",
            Domain.EXECUTION,
            "Trade Risk Gate",
            "Per-order validation: min R:R, max stop distance, max leverage.",
            TradeRiskGateParams,
            {"order": PortSpec("order", (OrderRequest,))},
            {"decision": PortSpec("decision", (PolicyDecision,))},
            _trade_risk_gate_factory,
        )
    )
    register_node(
        NodeSpec(
            "exec.portfolio_risk_gate",
            Domain.EXECUTION,
            "Portfolio Risk Gate",
            "Account-level guardrails: daily drawdown lockout, concurrent + margin caps.",
            PortfolioRiskGateParams,
            {"order": PortSpec("order", (OrderRequest,))},
            {"decision": PortSpec("decision", (PolicyDecision,))},
            _portfolio_risk_gate_factory,
        )
    )
    register_node(
        NodeSpec(
            "exec.execution_router",
            Domain.EXECUTION,
            "Execution Router",
            "Emit the terminal OrderRequest iff all risk-gate approvals pass (paper/sandbox only).",
            ExecutionRouterParams,
            {
                "order": PortSpec("order", (OrderRequest,)),
                "approvals": PortSpec(
                    "approvals",
                    (PolicyDecision,),
                    max_connections=None,
                ),
            },
            {"order": PortSpec("order", (OrderRequest,), terminal=True)},
            _execution_router_factory,
        )
    )


_register()
