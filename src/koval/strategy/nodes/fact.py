"""FACT detector nodes. Each wraps a pure helper and emits a MarketEvent,
preserving price_levels and strength the legacy registry discarded.

These mirror the legacy ``signal.*`` block factories 1:1 (same helpers, same
guards, same direction mapping) so the compatibility compiler can map
``signal.X`` -> ``fact.X`` and the golden regression stays behaviour-identical.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import MarketEvent
from koval.strategy.graph.indicators import ema_pair, rsi_pair
from koval.strategy.graph.node import BarContext, NodeEvaluate, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.helpers.signals.candlesticks import (
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_hammer,
    is_shooting_star,
)
from koval.strategy.helpers.signals.smc import (
    detect_bos,
    detect_choch,
    detect_fvg,
    detect_ob,
)
from koval.strategy.schemas import (
    BearishEngulfingParams,
    BosParams,
    BullishEngulfingParams,
    ChochParams,
    EmaCrossParams,
    EveryBarSignalParams,
    FvgParams,
    HammerParams,
    ObParams,
    RsiCrossParams,
    ShootingStarParams,
)

_EVENT_OUT = {"event": PortSpec("event", (MarketEvent,))}


def _has(arr: Any, n: int) -> bool:
    return arr is not None and len(arr) >= n


def _event(
    ctx: BarContext,
    kind: str,
    direction: str | None,
    strength: float | None = None,
    levels: list[float] | None = None,
) -> MarketEvent:
    return MarketEvent(
        bar_index=ctx.bar_index,
        timestamp_ms=ctx.timestamp_ms,
        source_node_id=kind,
        kind=kind,
        direction=direction,
        strength=strength,
        price_levels=levels or [],
    )


# ---------------------------------------------------------------------------
# SMC detectors
# ---------------------------------------------------------------------------


def _bos_factory(p: BosParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if not (
            _has(ctx.highs, p.lookback + 1)
            and _has(ctx.lows, p.lookback + 1)
            and _has(ctx.closes, p.lookback + 1)
        ):
            return {"event": None}
        d = detect_bos(ctx.highs, ctx.lows, ctx.closes, lookback=p.lookback)
        if d == "none":
            return {"event": None}
        level = float(
            ctx.highs[-(p.lookback + 1) : -1].max()
            if d == "bullish"
            else ctx.lows[-(p.lookback + 1) : -1].min()
        )
        return {"event": _event(ctx, "bos", d, levels=[level])}

    return evaluate


def _choch_factory(p: ChochParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if not (
            _has(ctx.highs, p.lookback + 2)
            and _has(ctx.lows, p.lookback + 2)
            and _has(ctx.closes, p.lookback + 2)
        ):
            return {"event": None}
        d = detect_choch(ctx.highs, ctx.lows, ctx.closes, lookback=p.lookback)
        return {"event": None if d == "none" else _event(ctx, "choch", d)}

    return evaluate


def _fvg_factory(_p: FvgParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if not (_has(ctx.highs, 3) and _has(ctx.lows, 3)):
            return {"event": None}
        gap = detect_fvg(ctx.highs, ctx.lows)
        if gap is None:
            return {"event": None}
        gap_low, gap_high = gap
        close = float(ctx.close)
        if gap_low > close:
            direction = "bearish"
        elif gap_high < close:
            direction = "bullish"
        else:
            return {"event": None}
        equilibrium = (gap_low + gap_high) / 2.0
        return {
            "event": _event(ctx, "fvg_created", direction, levels=[gap_low, gap_high, equilibrium])
        }

    return evaluate


def _ob_factory(p: ObParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if not (
            _has(ctx.opens, p.lookback + 1)
            and _has(ctx.highs, p.lookback + 1)
            and _has(ctx.lows, p.lookback + 1)
            and _has(ctx.closes, p.lookback + 1)
        ):
            return {"event": None}
        ob = detect_ob(ctx.opens, ctx.highs, ctx.lows, ctx.closes, lookback=p.lookback)
        if ob is None:
            return {"event": None}
        ob_low, ob_high, direction = ob
        return {"event": _event(ctx, "order_block", direction, levels=[ob_low, ob_high])}

    return evaluate


# ---------------------------------------------------------------------------
# Candlestick patterns
# ---------------------------------------------------------------------------


def _hammer_factory(p: HammerParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if is_hammer(ctx.open, ctx.high, ctx.low, ctx.close, body_ratio=p.body_ratio):
            return {"event": _event(ctx, "hammer", "bullish")}
        return {"event": None}

    return evaluate


def _shooting_star_factory(p: ShootingStarParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if is_shooting_star(ctx.open, ctx.high, ctx.low, ctx.close, body_ratio=p.body_ratio):
            return {"event": _event(ctx, "shooting_star", "bearish")}
        return {"event": None}

    return evaluate


def _bullish_engulfing_factory(_p: BullishEngulfingParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if not (_has(ctx.opens, 2) and _has(ctx.closes, 2)):
            return {"event": None}
        if is_bullish_engulfing(
            prev_open=float(ctx.opens[-2]),
            prev_close=float(ctx.closes[-2]),
            curr_open=float(ctx.open),
            curr_close=float(ctx.close),
        ):
            return {"event": _event(ctx, "bullish_engulfing", "bullish")}
        return {"event": None}

    return evaluate


def _bearish_engulfing_factory(_p: BearishEngulfingParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if not (_has(ctx.opens, 2) and _has(ctx.closes, 2)):
            return {"event": None}
        if is_bearish_engulfing(
            prev_open=float(ctx.opens[-2]),
            prev_close=float(ctx.closes[-2]),
            curr_open=float(ctx.open),
            curr_close=float(ctx.close),
        ):
            return {"event": _event(ctx, "bearish_engulfing", "bearish")}
        return {"event": None}

    return evaluate


def _every_bar_factory(p: EveryBarSignalParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        return {"event": _event(ctx, "every_bar", p.direction)}

    return evaluate


# ---------------------------------------------------------------------------
# Technical crosses
# ---------------------------------------------------------------------------


def _ema_cross_factory(p: EmaCrossParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if not _has(ctx.closes, p.slow + 1):
            return {"event": None}
        prev_fast, curr_fast = ema_pair(ctx, p.fast)
        prev_slow, curr_slow = ema_pair(ctx, p.slow)
        if np.isnan(prev_fast) or np.isnan(prev_slow):
            return {"event": None}
        prev_above, curr_above = prev_fast > prev_slow, curr_fast > curr_slow
        d = (
            "bullish"
            if not prev_above and curr_above
            else "bearish"
            if prev_above and not curr_above
            else "none"
        )
        return {"event": None if d == "none" else _event(ctx, "ema_cross", d)}

    return evaluate


def _rsi_cross_factory(p: RsiCrossParams) -> NodeEvaluate:
    def evaluate(ctx, inputs, state):
        if not _has(ctx.closes, p.period + 2):
            return {"event": None}
        previous, current = rsi_pair(ctx, p.period)
        crossed = (
            previous < p.level <= current
            if p.direction == "cross_up"
            else previous > p.level >= current
        )
        if not crossed:
            return {"event": None}
        d = "bullish" if p.direction == "cross_up" else "bearish"
        return {"event": _event(ctx, "rsi_cross", d)}

    return evaluate


def _register() -> None:
    register_node(
        NodeSpec(
            "fact.bos",
            Domain.FACT,
            "Break of Structure",
            "Close breaks recent swing; emits broken level.",
            BosParams,
            {},
            _EVENT_OUT,
            _bos_factory,
        )
    )
    register_node(
        NodeSpec(
            "fact.choch",
            Domain.FACT,
            "Change of Character",
            "Reversal break against the established trend.",
            ChochParams,
            {},
            _EVENT_OUT,
            _choch_factory,
        )
    )
    register_node(
        NodeSpec(
            "fact.fvg",
            Domain.FACT,
            "Fair Value Gap",
            "3-bar imbalance; emits [low, high, equilibrium].",
            FvgParams,
            {},
            _EVENT_OUT,
            _fvg_factory,
        )
    )
    register_node(
        NodeSpec(
            "fact.ob",
            Domain.FACT,
            "Order Block",
            "Last opposing candle before a push; emits [low, high].",
            ObParams,
            {},
            _EVENT_OUT,
            _ob_factory,
        )
    )
    register_node(
        NodeSpec(
            "fact.hammer",
            Domain.FACT,
            "Hammer",
            "Long lower wick + small body on the current bar -> bullish.",
            HammerParams,
            {},
            _EVENT_OUT,
            _hammer_factory,
        )
    )
    register_node(
        NodeSpec(
            "fact.shooting_star",
            Domain.FACT,
            "Shooting Star",
            "Long upper wick + small body on the current bar -> bearish.",
            ShootingStarParams,
            {},
            _EVENT_OUT,
            _shooting_star_factory,
        )
    )
    register_node(
        NodeSpec(
            "fact.bullish_engulfing",
            Domain.FACT,
            "Bullish Engulfing",
            "Current bullish candle fully engulfs the previous bearish body.",
            BullishEngulfingParams,
            {},
            _EVENT_OUT,
            _bullish_engulfing_factory,
        )
    )
    register_node(
        NodeSpec(
            "fact.bearish_engulfing",
            Domain.FACT,
            "Bearish Engulfing",
            "Current bearish candle fully engulfs the previous bullish body.",
            BearishEngulfingParams,
            {},
            _EVENT_OUT,
            _bearish_engulfing_factory,
        )
    )
    register_node(
        NodeSpec(
            "fact.every_bar",
            Domain.FACT,
            "Every Bar (Paper Test)",
            "Diagnostic paper-test event emitted on every closed bar.",
            EveryBarSignalParams,
            {},
            _EVENT_OUT,
            _every_bar_factory,
        )
    )
    register_node(
        NodeSpec(
            "fact.ema_cross",
            Domain.FACT,
            "EMA Cross",
            "Fast EMA crossing slow EMA on the last bar (golden / death cross).",
            EmaCrossParams,
            {},
            _EVENT_OUT,
            _ema_cross_factory,
        )
    )
    register_node(
        NodeSpec(
            "fact.rsi_cross",
            Domain.FACT,
            "RSI Cross",
            "RSI crossing a level on the last bar; direction follows cross_up / cross_down.",
            RsiCrossParams,
            {},
            _EVENT_OUT,
            _rsi_cross_factory,
        )
    )


_register()
