"""Block catalog and strategy registry.

Two parallel registries live in this module:

* `BLOCK_CATALOG`  — building blocks (signals, filters, entries, exits, risk)
                     keyed by `BlockSpec.type`. The block assembler walks this.
* `STRATEGY_REGISTRY` — an extension registry keyed by
                        `StrategyDefinition.name`. Callers register complete
                        strategy classes explicitly with `register_strategy`.
                        Bundled graph preset builders are not registry entries.

Wrapping helpers in `factory(params) -> bound_callable` is the only place
where adapter logic touches the helpers. The block assembler stays generic.

Per-category contract for the value `factory(params)` returns:
  signal       : (strategy) -> "bullish" | "bearish" | "none"
  filter       : (strategy) -> bool
  entry        : EntryConfig (returned directly — entry blocks are config blocks,
                              not bar-by-bar callables)
  exit         : (strategy, direction, entry_price) -> tuple[float, float | None]
  dynamic_exit : (strategy, *, trade_id, direction, entry_price, current_stop)
                 -> float | None
  risk         : (strategy, entry_price, stop_loss, direction) -> float
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import EntryConfig
from koval.strategy.helpers.exits.breakeven import breakeven_trigger
from koval.strategy.helpers.exits.fixed import fixed_sl_tp
from koval.strategy.helpers.exits.trailing import trailing_stop_price
from koval.strategy.helpers.filters.momentum import (
    macd_filter,
    rsi_filter,
    stoch_filter,
)
from koval.strategy.helpers.filters.trend import adx_filter, ema_trend_filter
from koval.strategy.helpers.filters.volatility import (
    atr_volatility_filter,
    bb_volatility_filter,
)
from koval.strategy.helpers.risk.position_sizer import calculate_position_size
from koval.strategy.helpers.signals.candlesticks import (
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_doji,
    is_hammer,
    is_shooting_star,
)
from koval.strategy.helpers.signals.smc import (
    detect_bos,
    detect_choch,
    detect_fvg,
    detect_ob,
)
from koval.strategy.helpers.signals.technical import ema_cross, rsi_cross
from koval.strategy.schemas import (
    AdxFilterParams,
    AtrVolatilityParams,
    BbVolatilityParams,
    BearishEngulfingParams,
    BosParams,
    BreakevenParams,
    BullishEngulfingParams,
    ChochParams,
    EmaCrossParams,
    EmaTrendFilterParams,
    EntryParams,
    EveryBarSignalParams,
    FixedSlTpParams,
    FvgParams,
    HammerParams,
    IsDojiFilterParams,
    MacdFilterParams,
    ObParams,
    PctRiskParams,
    RsiCrossParams,
    RsiFilterParams,
    ShootingStarParams,
    StochFilterParams,
    TrailingStopParams,
)

Category = Literal["signal", "filter", "entry", "exit", "dynamic_exit", "risk"]


@dataclass(frozen=True)
class BlockSpec:
    """A registered block.

    `factory(params)` returns either a callable bound to the helper, or — for
    entry blocks — an `EntryConfig` directly. See module docstring for the
    per-category contract.
    """

    type: str
    category: Category
    display_name: str
    description: str
    params_schema: type[BaseModel]
    factory: Callable[[BaseModel], Any]


BLOCK_CATALOG: dict[str, BlockSpec] = {}


def register_block(spec: BlockSpec) -> None:
    if spec.type in BLOCK_CATALOG:
        raise ValueError(f"Block {spec.type!r} already registered")
    BLOCK_CATALOG[spec.type] = spec


def get_block(block_type: str) -> BlockSpec:
    if block_type not in BLOCK_CATALOG:
        raise KeyError(block_type)
    return BLOCK_CATALOG[block_type]


def get_all_blocks() -> list[BlockSpec]:
    return list(BLOCK_CATALOG.values())


# ---------------------------------------------------------------------------
# Strategy extension registry (one class + its top-level params schema)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyDefinition:
    """An explicitly registered strategy extension.

    Held by `STRATEGY_REGISTRY` and surfaced by `get_all_definitions()`.
    Registration is opt-in; importing `koval.strategy.presets` does not mutate
    this registry. The class plus its params schema is the full contract — the
    runtime instantiates the class and injects validated params via
    `params_schema(**default_params)` (or the user's overrides).
    """

    name: str
    display_name: str
    description: str
    strategy_class: type[DeclarativeStrategy]
    params_schema: type[BaseModel]
    default_params: dict[str, Any]
    tags: list[str]


STRATEGY_REGISTRY: dict[str, StrategyDefinition] = {}


def register_strategy(sd: StrategyDefinition) -> None:
    if sd.name in STRATEGY_REGISTRY:
        raise ValueError(f"Strategy {sd.name!r} already registered")
    STRATEGY_REGISTRY[sd.name] = sd


def get_strategy(name: str) -> StrategyDefinition:
    if name not in STRATEGY_REGISTRY:
        raise KeyError(name)
    return STRATEGY_REGISTRY[name]


def get_all_definitions() -> list[StrategyDefinition]:
    return list(STRATEGY_REGISTRY.values())


# ---------------------------------------------------------------------------
# Helpers used by factories
# ---------------------------------------------------------------------------


def _has_array(arr: Any, min_len: int = 1) -> bool:
    return arr is not None and len(arr) >= min_len


# ---------------------------------------------------------------------------
# Signal factories
# ---------------------------------------------------------------------------


def _signal_bos(p: BosParams) -> Callable[[Any], str]:
    def bound(s: Any) -> str:
        if not (
            _has_array(s.highs, p.lookback + 1)
            and _has_array(s.lows, p.lookback + 1)
            and _has_array(s.closes, p.lookback + 1)
        ):
            return "none"
        return detect_bos(s.highs, s.lows, s.closes, lookback=p.lookback)

    return bound


def _signal_choch(p: ChochParams) -> Callable[[Any], str]:
    def bound(s: Any) -> str:
        if not (
            _has_array(s.highs, p.lookback + 2)
            and _has_array(s.lows, p.lookback + 2)
            and _has_array(s.closes, p.lookback + 2)
        ):
            return "none"
        return detect_choch(s.highs, s.lows, s.closes, lookback=p.lookback)

    return bound


def _signal_fvg(_p: FvgParams) -> Callable[[Any], str]:
    """Map detect_fvg's (gap_low, gap_high) tuple to a direction.

    Gap fully above current close → unfilled supply zone → "bearish".
    Gap fully below current close → unfilled demand zone → "bullish".
    Gap straddles close (or no gap) → "none".
    """

    def bound(s: Any) -> str:
        if not (_has_array(s.highs, 3) and _has_array(s.lows, 3)):
            return "none"
        gap = detect_fvg(s.highs, s.lows)
        if gap is None:
            return "none"
        gap_low, gap_high = gap
        close = float(s.close)
        if gap_low > close:
            return "bearish"
        if gap_high < close:
            return "bullish"
        return "none"

    return bound


def _signal_ob(p: ObParams) -> Callable[[Any], str]:
    def bound(s: Any) -> str:
        if not (
            _has_array(s.opens, p.lookback + 1)
            and _has_array(s.highs, p.lookback + 1)
            and _has_array(s.lows, p.lookback + 1)
            and _has_array(s.closes, p.lookback + 1)
        ):
            return "none"
        ob = detect_ob(s.opens, s.highs, s.lows, s.closes, lookback=p.lookback)
        if ob is None:
            return "none"
        return ob[2]

    return bound


def _signal_hammer(p: HammerParams) -> Callable[[Any], str]:
    def bound(s: Any) -> str:
        return (
            "bullish"
            if is_hammer(s.open, s.high, s.low, s.close, body_ratio=p.body_ratio)
            else "none"
        )

    return bound


def _signal_shooting_star(p: ShootingStarParams) -> Callable[[Any], str]:
    def bound(s: Any) -> str:
        return (
            "bearish"
            if is_shooting_star(s.open, s.high, s.low, s.close, body_ratio=p.body_ratio)
            else "none"
        )

    return bound


def _signal_bullish_engulfing(_p: BullishEngulfingParams) -> Callable[[Any], str]:
    def bound(s: Any) -> str:
        if not (_has_array(s.opens, 2) and _has_array(s.closes, 2)):
            return "none"
        triggered = is_bullish_engulfing(
            prev_open=float(s.opens[-2]),
            prev_close=float(s.closes[-2]),
            curr_open=float(s.open),
            curr_close=float(s.close),
        )
        return "bullish" if triggered else "none"

    return bound


def _signal_bearish_engulfing(_p: BearishEngulfingParams) -> Callable[[Any], str]:
    def bound(s: Any) -> str:
        if not (_has_array(s.opens, 2) and _has_array(s.closes, 2)):
            return "none"
        triggered = is_bearish_engulfing(
            prev_open=float(s.opens[-2]),
            prev_close=float(s.closes[-2]),
            curr_open=float(s.open),
            curr_close=float(s.close),
        )
        return "bearish" if triggered else "none"

    return bound


def _signal_every_bar(p: EveryBarSignalParams) -> Callable[[Any], str]:
    def bound(_s: Any) -> str:
        return p.direction

    return bound


def _signal_ema_cross(p: EmaCrossParams) -> Callable[[Any], str]:
    def bound(s: Any) -> str:
        if not _has_array(s.closes, p.slow + 1):
            return "none"
        return ema_cross(s.closes, fast=p.fast, slow=p.slow)

    return bound


def _signal_rsi_cross(p: RsiCrossParams) -> Callable[[Any], str]:
    def bound(s: Any) -> str:
        if not _has_array(s.closes, p.period + 2):
            return "none"
        crossed = rsi_cross(s.closes, period=p.period, level=p.level, direction=p.direction)
        if not crossed:
            return "none"
        return "bullish" if p.direction == "cross_up" else "bearish"

    return bound


# ---------------------------------------------------------------------------
# Filter factories
# ---------------------------------------------------------------------------


def _filter_rsi(p: RsiFilterParams) -> Callable[[Any], bool]:
    def bound(s: Any) -> bool:
        if not _has_array(s.closes, p.period + 1):
            return False
        return rsi_filter(s.closes, period=p.period, min_val=p.min_val, max_val=p.max_val)

    return bound


def _filter_stoch(p: StochFilterParams) -> Callable[[Any], bool]:
    def bound(s: Any) -> bool:
        if not (
            _has_array(s.highs, p.k_period)
            and _has_array(s.lows, p.k_period)
            and _has_array(s.closes, p.k_period)
        ):
            return False
        return stoch_filter(
            s.highs,
            s.lows,
            s.closes,
            k_period=p.k_period,
            min_val=p.min_val,
            max_val=p.max_val,
        )

    return bound


def _filter_macd(p: MacdFilterParams) -> Callable[[Any], bool]:
    def bound(s: Any) -> bool:
        if not _has_array(s.closes, p.slow + p.signal_period):
            return False
        return macd_filter(
            s.closes,
            fast=p.fast,
            slow=p.slow,
            signal_period=p.signal_period,
            require_positive=p.require_positive,
        )

    return bound


def _filter_adx(p: AdxFilterParams) -> Callable[[Any], bool]:
    def bound(s: Any) -> bool:
        if not (
            _has_array(s.highs, p.period * 2 + 1)
            and _has_array(s.lows, p.period * 2 + 1)
            and _has_array(s.closes, p.period * 2 + 1)
        ):
            return False
        return adx_filter(s.highs, s.lows, s.closes, period=p.period, min_adx=p.min_adx)

    return bound


def _filter_ema_trend(p: EmaTrendFilterParams) -> Callable[[Any], bool]:
    def bound(s: Any) -> bool:
        if not _has_array(s.closes, p.period):
            return False
        return ema_trend_filter(s.closes, period=p.period, direction=p.direction)

    return bound


def _filter_atr_volatility(p: AtrVolatilityParams) -> Callable[[Any], bool]:
    def bound(s: Any) -> bool:
        if not (
            _has_array(s.highs, p.period + 1)
            and _has_array(s.lows, p.period + 1)
            and _has_array(s.closes, p.period + 1)
        ):
            return False
        return atr_volatility_filter(
            s.highs, s.lows, s.closes, period=p.period, min_atr_pct=p.min_atr_pct
        )

    return bound


def _filter_bb_volatility(p: BbVolatilityParams) -> Callable[[Any], bool]:
    def bound(s: Any) -> bool:
        if not _has_array(s.closes, p.period):
            return False
        return bb_volatility_filter(
            s.closes,
            period=p.period,
            std_dev=p.std_dev,
            min_bandwidth_pct=p.min_bandwidth_pct,
        )

    return bound


def _filter_is_doji(p: IsDojiFilterParams) -> Callable[[Any], bool]:
    def bound(s: Any) -> bool:
        return is_doji(s.open, s.high, s.low, s.close, body_pct=p.body_pct)

    return bound


# ---------------------------------------------------------------------------
# Entry factories — return EntryConfig directly (config block, not callable)
# ---------------------------------------------------------------------------


def _entry_long_only(p: EntryParams) -> EntryConfig:
    return EntryConfig(allow_long=True, allow_short=False, entry_type=p.entry_type)


def _entry_short_only(p: EntryParams) -> EntryConfig:
    return EntryConfig(allow_long=False, allow_short=True, entry_type=p.entry_type)


def _entry_both(p: EntryParams) -> EntryConfig:
    return EntryConfig(allow_long=True, allow_short=True, entry_type=p.entry_type)


# ---------------------------------------------------------------------------
# Exit factories (initial bracket)
# ---------------------------------------------------------------------------


def _exit_fixed_sl_tp(
    p: FixedSlTpParams,
) -> Callable[[Any, str, float], tuple[float, float | None]]:
    def bound(_s: Any, direction: str, entry_price: float) -> tuple[float, float | None]:
        sl, tp = fixed_sl_tp(
            entry_price=entry_price,
            direction=direction,
            sl_pct=p.sl_pct,
            risk_reward=p.risk_reward,
            tp_pct=p.tp_pct,
        )
        return sl, tp

    return bound


# ---------------------------------------------------------------------------
# Dynamic-exit factories
# ---------------------------------------------------------------------------


def _exit_trailing_stop(p: TrailingStopParams) -> Callable[..., float | None]:
    def bound(
        s: Any,
        *,
        trade_id: int,
        direction: str,
        entry_price: float,
        current_stop: float,
    ) -> float | None:
        # `trade_id` and `entry_price` are part of the dynamic-exit contract
        # but unused here — trailing-stop only needs current price + current stop.
        del trade_id, entry_price
        new_stop = trailing_stop_price(
            direction=direction,
            current_price=float(s.close),
            trail_pct=p.trail_pct,
            current_stop=current_stop,
        )
        # Only return when the stop actually moves (otherwise: no update).
        if new_stop == current_stop:
            return None
        return float(new_stop)

    return bound


def _exit_breakeven(p: BreakevenParams) -> Callable[..., float | None]:
    def bound(
        s: Any,
        *,
        trade_id: int,
        direction: str,
        entry_price: float,
        current_stop: float,
    ) -> float | None:
        del trade_id  # part of the contract, unused here
        return breakeven_trigger(
            entry_price=entry_price,
            direction=direction,
            current_price=float(s.close),
            stop_loss=current_stop,
            trigger_r=p.trigger_r,
        )

    return bound


# ---------------------------------------------------------------------------
# Risk factory
# ---------------------------------------------------------------------------


def _risk_pct(p: PctRiskParams) -> Callable[[Any, float, float, str], float]:
    def bound(s: Any, entry_price: float, stop_loss: float, direction: str) -> float:
        return calculate_position_size(
            account_value=float(s.account_value),
            risk_per_trade_pct=p.risk_pct,
            entry_price=entry_price,
            stop_loss=stop_loss,
            direction=direction,
            leverage=p.leverage,
        )

    return bound


# ---------------------------------------------------------------------------
# Registration — module import populates the catalog
# ---------------------------------------------------------------------------


def _seed_catalog() -> None:
    specs: list[BlockSpec] = [
        # --- signals ---
        BlockSpec(
            type="signal.bos",
            category="signal",
            display_name="Break of Structure",
            description="Close breaks above recent swing high (bullish) or below recent swing low (bearish).",
            params_schema=BosParams,
            factory=_signal_bos,
        ),
        BlockSpec(
            type="signal.choch",
            category="signal",
            display_name="Change of Character",
            description="Reversal break — current close breaks structure against the established trend.",
            params_schema=ChochParams,
            factory=_signal_choch,
        ),
        BlockSpec(
            type="signal.fvg",
            category="signal",
            display_name="Fair Value Gap",
            description="3-bar imbalance gap. Above close → bearish supply; below close → bullish demand.",
            params_schema=FvgParams,
            factory=_signal_fvg,
        ),
        BlockSpec(
            type="signal.ob",
            category="signal",
            display_name="Order Block",
            description="Last opposing candle before a bullish/bearish push; direction lifted from the helper.",
            params_schema=ObParams,
            factory=_signal_ob,
        ),
        BlockSpec(
            type="signal.hammer",
            category="signal",
            display_name="Hammer",
            description="Long lower wick + small body on the current bar → bullish.",
            params_schema=HammerParams,
            factory=_signal_hammer,
        ),
        BlockSpec(
            type="signal.shooting_star",
            category="signal",
            display_name="Shooting Star",
            description="Long upper wick + small body on the current bar → bearish.",
            params_schema=ShootingStarParams,
            factory=_signal_shooting_star,
        ),
        BlockSpec(
            type="signal.bullish_engulfing",
            category="signal",
            display_name="Bullish Engulfing",
            description="Current bullish candle fully engulfs previous bearish body.",
            params_schema=BullishEngulfingParams,
            factory=_signal_bullish_engulfing,
        ),
        BlockSpec(
            type="signal.bearish_engulfing",
            category="signal",
            display_name="Bearish Engulfing",
            description="Current bearish candle fully engulfs previous bullish body.",
            params_schema=BearishEngulfingParams,
            factory=_signal_bearish_engulfing,
        ),
        BlockSpec(
            type="signal.every_bar",
            category="signal",
            display_name="Every Bar (Paper Test)",
            description="Diagnostic paper-test signal that emits on every closed bar.",
            params_schema=EveryBarSignalParams,
            factory=_signal_every_bar,
        ),
        BlockSpec(
            type="signal.ema_cross",
            category="signal",
            display_name="EMA Cross",
            description="Fast EMA crossing slow EMA on the last bar (golden / death cross).",
            params_schema=EmaCrossParams,
            factory=_signal_ema_cross,
        ),
        BlockSpec(
            type="signal.rsi_cross",
            category="signal",
            display_name="RSI Cross",
            description="RSI crossing a level on the last bar; direction follows cross_up / cross_down.",
            params_schema=RsiCrossParams,
            factory=_signal_rsi_cross,
        ),
        # --- filters ---
        BlockSpec(
            type="filter.rsi",
            category="filter",
            display_name="RSI Band Filter",
            description="True when current RSI is within [min_val, max_val].",
            params_schema=RsiFilterParams,
            factory=_filter_rsi,
        ),
        BlockSpec(
            type="filter.stoch",
            category="filter",
            display_name="Stochastic Band Filter",
            description="True when Stochastic %K is within [min_val, max_val].",
            params_schema=StochFilterParams,
            factory=_filter_stoch,
        ),
        BlockSpec(
            type="filter.macd",
            category="filter",
            display_name="MACD Sign Filter",
            description="True when MACD histogram sign matches require_positive.",
            params_schema=MacdFilterParams,
            factory=_filter_macd,
        ),
        BlockSpec(
            type="filter.adx",
            category="filter",
            display_name="ADX Strength Filter",
            description="True when Wilder-smoothed ADX >= min_adx.",
            params_schema=AdxFilterParams,
            factory=_filter_adx,
        ),
        BlockSpec(
            type="filter.ema_trend",
            category="filter",
            display_name="EMA Trend Filter",
            description="True when current close is on the configured side of the EMA.",
            params_schema=EmaTrendFilterParams,
            factory=_filter_ema_trend,
        ),
        BlockSpec(
            type="filter.atr_volatility",
            category="filter",
            display_name="ATR Volatility Floor",
            description="True when ATR / close * 100 >= min_atr_pct.",
            params_schema=AtrVolatilityParams,
            factory=_filter_atr_volatility,
        ),
        BlockSpec(
            type="filter.bb_volatility",
            category="filter",
            display_name="Bollinger Bandwidth Floor",
            description="True when Bollinger bandwidth (% of mean) >= min_bandwidth_pct.",
            params_schema=BbVolatilityParams,
            factory=_filter_bb_volatility,
        ),
        BlockSpec(
            type="filter.is_doji",
            category="filter",
            display_name="Doji Bar Filter",
            description="True when current bar's body / range <= body_pct.",
            params_schema=IsDojiFilterParams,
            factory=_filter_is_doji,
        ),
        # --- entry ---
        BlockSpec(
            type="entry.long_only",
            category="entry",
            display_name="Long Only",
            description="Allow long entries; block shorts.",
            params_schema=EntryParams,
            factory=_entry_long_only,
        ),
        BlockSpec(
            type="entry.short_only",
            category="entry",
            display_name="Short Only",
            description="Allow short entries; block longs.",
            params_schema=EntryParams,
            factory=_entry_short_only,
        ),
        BlockSpec(
            type="entry.both",
            category="entry",
            display_name="Long & Short",
            description="Allow both long and short entries.",
            params_schema=EntryParams,
            factory=_entry_both,
        ),
        # --- exits (initial bracket) ---
        BlockSpec(
            type="exit.fixed_sl_tp",
            category="exit",
            display_name="Fixed SL / TP",
            description="Stop loss as % of entry; TP from risk_reward unless tp_pct overrides.",
            params_schema=FixedSlTpParams,
            factory=_exit_fixed_sl_tp,
        ),
        # --- dynamic exits ---
        BlockSpec(
            type="exit.trailing_stop",
            category="dynamic_exit",
            display_name="Trailing Stop",
            description="Move stop with price by trail_pct; never against the position.",
            params_schema=TrailingStopParams,
            factory=_exit_trailing_stop,
        ),
        BlockSpec(
            type="exit.breakeven",
            category="dynamic_exit",
            display_name="Move to Breakeven",
            description="Move SL to entry once price has travelled trigger_r × initial risk in profit.",
            params_schema=BreakevenParams,
            factory=_exit_breakeven,
        ),
        # --- risk ---
        BlockSpec(
            type="risk.pct_risk",
            category="risk",
            display_name="Fixed-Percent Risk",
            description="Size = (equity * risk_pct/100) / |entry - SL| * leverage.",
            params_schema=PctRiskParams,
            factory=_risk_pct,
        ),
    ]
    for spec in specs:
        register_block(spec)


_seed_catalog()
