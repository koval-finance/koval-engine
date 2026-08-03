"""STATE seed nodes. Persistent market state carried across bars via NodeState,
proving the state substrate. Reuse trend/volatility helpers."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import MarketEvent, MarketState
from koval.strategy.graph.node import BarContext, NodeEvaluate, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.helpers.filters.trend import ema_trend_filter
from koval.strategy.helpers.filters.volatility import atr_volatility_filter
from koval.strategy.schemas import _StrictModel

_CONTEXT_IN = {
    "context": PortSpec(
        "context",
        (MarketEvent, MarketState),
        required=False,
        max_connections=None,
    )
}
_STATE_OUT = {"state": PortSpec("state", (MarketState,))}


class TrendBiasParams(_StrictModel):
    period: int = Field(50, ge=2)


class VolatilityRegimeParams(_StrictModel):
    period: int = Field(14, ge=2)
    min_atr_pct: float = Field(0.5, ge=0)


def _has(arr: Any, n: int) -> bool:
    return arr is not None and len(arr) >= n


def _state(ctx: BarContext, kind: str, status: str, state: dict) -> MarketState:
    if state.get("status") != status:
        state["status"] = status
        state["since_bar"] = ctx.bar_index
    return MarketState(
        bar_index=ctx.bar_index,
        timestamp_ms=ctx.timestamp_ms,
        source_node_id=kind,
        kind=kind,
        status=status,
        since_bar=state["since_bar"],
    )


def _trend_bias_factory(p: TrendBiasParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if not _has(ctx.closes, p.period):
            return {"state": _state(ctx, "trend_bias", "neutral", state)}
        if ema_trend_filter(ctx.closes, period=p.period, direction="bullish"):
            status = "bullish"
        elif ema_trend_filter(ctx.closes, period=p.period, direction="bearish"):
            status = "bearish"
        else:
            status = "neutral"
        return {"state": _state(ctx, "trend_bias", status, state)}

    return evaluate


def _volatility_regime_factory(p: VolatilityRegimeParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if not (
            _has(ctx.highs, p.period + 1)
            and _has(ctx.lows, p.period + 1)
            and _has(ctx.closes, p.period + 1)
        ):
            return {"state": _state(ctx, "volatility_regime", "normal", state)}
        expanded = atr_volatility_filter(
            ctx.highs, ctx.lows, ctx.closes, period=p.period, min_atr_pct=p.min_atr_pct
        )
        status = "expansion" if expanded else "compression"
        return {"state": _state(ctx, "volatility_regime", status, state)}

    return evaluate


def _register() -> None:
    register_node(
        NodeSpec(
            "state.trend_bias",
            Domain.STATE,
            "Trend Bias",
            "Persistent trend direction with since_bar tracking.",
            TrendBiasParams,
            _CONTEXT_IN,
            _STATE_OUT,
            _trend_bias_factory,
        )
    )
    register_node(
        NodeSpec(
            "state.volatility_regime",
            Domain.STATE,
            "Volatility Regime",
            "Compression / expansion regime across bars.",
            VolatilityRegimeParams,
            _CONTEXT_IN,
            _STATE_OUT,
            _volatility_regime_factory,
        )
    )


_register()
