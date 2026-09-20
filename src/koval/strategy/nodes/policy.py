"""POLICY filter nodes. Each wraps an existing filter helper and emits a
PolicyDecision(allowed=...). Mirrors the legacy filter blocks 1:1 so the compat
compiler can map filter.* -> policy.* directly."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import MarketEvent, MarketState, PolicyDecision
from koval.strategy.graph.indicators import atr_allowed, ema_trend
from koval.strategy.graph.node import BarContext, NodeEvaluate, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.helpers.filters.momentum import macd_filter, rsi_filter, stoch_filter
from koval.strategy.helpers.filters.trend import adx_filter
from koval.strategy.helpers.filters.volatility import (
    bb_volatility_filter,
)
from koval.strategy.helpers.signals.candlesticks import is_doji
from koval.strategy.schemas import (
    AdxFilterParams,
    AtrVolatilityParams,
    BbVolatilityParams,
    EmaTrendFilterParams,
    IsDojiFilterParams,
    MacdFilterParams,
    RsiFilterParams,
    StochFilterParams,
)

_CONTEXT_IN = {
    "context": PortSpec(
        "context",
        (MarketEvent, MarketState),
        required=False,
        max_connections=None,
    )
}
_POLICY_OUT = {"policy": PortSpec("policy", (PolicyDecision,))}


def _has(arr: Any, n: int) -> bool:
    return arr is not None and len(arr) >= n


def _decision(ctx: BarContext, allowed: bool, reason: str | None) -> PolicyDecision:
    return PolicyDecision(
        bar_index=ctx.bar_index,
        timestamp_ms=ctx.timestamp_ms,
        source_node_id="policy",
        allowed=allowed,
        reason=None if allowed else reason,
        scope="trading",
    )


def _wrap(predicate: Callable[[BarContext, Any], bool], reason: str):
    def factory(p) -> NodeEvaluate:
        def evaluate(ctx, inputs, state):
            return {"policy": _decision(ctx, predicate(ctx, p), reason)}

        return evaluate

    return factory


def _register() -> None:
    register_node(
        NodeSpec(
            "policy.rsi",
            Domain.POLICY,
            "RSI Band",
            "",
            RsiFilterParams,
            _CONTEXT_IN,
            _POLICY_OUT,
            _wrap(
                lambda c, p: (
                    _has(c.closes, p.period + 1)
                    and rsi_filter(c.closes, period=p.period, min_val=p.min_val, max_val=p.max_val)
                ),
                "rsi_out_of_band",
            ),
        )
    )
    register_node(
        NodeSpec(
            "policy.adx",
            Domain.POLICY,
            "ADX Strength",
            "",
            AdxFilterParams,
            _CONTEXT_IN,
            _POLICY_OUT,
            _wrap(
                lambda c, p: (
                    _has(c.closes, p.period * 2 + 1)
                    and adx_filter(c.highs, c.lows, c.closes, period=p.period, min_adx=p.min_adx)
                ),
                "adx_too_low",
            ),
        )
    )
    register_node(
        NodeSpec(
            "policy.ema_trend",
            Domain.POLICY,
            "EMA Trend",
            "",
            EmaTrendFilterParams,
            _CONTEXT_IN,
            _POLICY_OUT,
            _wrap(
                lambda c, p: _has(c.closes, p.period) and ema_trend(c, p.period, p.direction),
                "wrong_side_of_ema",
            ),
        )
    )
    register_node(
        NodeSpec(
            "policy.atr_volatility",
            Domain.POLICY,
            "ATR Floor",
            "",
            AtrVolatilityParams,
            _CONTEXT_IN,
            _POLICY_OUT,
            _wrap(
                lambda c, p: (
                    _has(c.closes, p.period + 1) and atr_allowed(c, p.period, p.min_atr_pct)
                ),
                "volatility_too_low",
            ),
        )
    )
    register_node(
        NodeSpec(
            "policy.bb_volatility",
            Domain.POLICY,
            "BB Bandwidth Floor",
            "",
            BbVolatilityParams,
            _CONTEXT_IN,
            _POLICY_OUT,
            _wrap(
                lambda c, p: (
                    _has(c.closes, p.period)
                    and bb_volatility_filter(
                        c.closes,
                        period=p.period,
                        std_dev=p.std_dev,
                        min_bandwidth_pct=p.min_bandwidth_pct,
                    )
                ),
                "bandwidth_too_low",
            ),
        )
    )
    register_node(
        NodeSpec(
            "policy.stoch",
            Domain.POLICY,
            "Stochastic Band",
            "",
            StochFilterParams,
            _CONTEXT_IN,
            _POLICY_OUT,
            _wrap(
                lambda c, p: (
                    _has(c.closes, p.k_period)
                    and stoch_filter(
                        c.highs,
                        c.lows,
                        c.closes,
                        k_period=p.k_period,
                        min_val=p.min_val,
                        max_val=p.max_val,
                    )
                ),
                "stoch_out_of_band",
            ),
        )
    )
    register_node(
        NodeSpec(
            "policy.macd",
            Domain.POLICY,
            "MACD Sign",
            "",
            MacdFilterParams,
            _CONTEXT_IN,
            _POLICY_OUT,
            _wrap(
                lambda c, p: (
                    _has(c.closes, p.slow + p.signal_period)
                    and macd_filter(
                        c.closes,
                        fast=p.fast,
                        slow=p.slow,
                        signal_period=p.signal_period,
                        require_positive=p.require_positive,
                    )
                ),
                "macd_sign_mismatch",
            ),
        )
    )
    register_node(
        NodeSpec(
            "policy.is_doji",
            Domain.POLICY,
            "Doji Bar",
            "",
            IsDojiFilterParams,
            _CONTEXT_IN,
            _POLICY_OUT,
            _wrap(
                lambda c, p: is_doji(c.open, c.high, c.low, c.close, body_pct=p.body_pct),
                "not_a_doji",
            ),
        )
    )


_register()
