"""Pydantic v2 parameter models for every block in the catalog.

Single source of truth for:
- typed validation of UI-supplied params,
- defaults that mirror the helper signatures in `koval/strategy/helpers/`,
- JSON Schema available to hosts via `model_json_schema()`.

Each model uses `extra="forbid"` so unknown UI keys fail loudly rather than
being silently ignored.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from koval.engine.spot_numbers import InputNumber


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SpotParams(_StrictModel):
    @model_validator(mode="before")
    @classmethod
    def strict_version(cls, value):
        if isinstance(value, dict) and isinstance(value.get("version"), bool):
            raise ValueError("Spot version must be integer 1")
        return value


class SpotDcaParams(_SpotParams):
    """Bounded price-drop basket; all cash/cost assumptions are explicit."""

    version: Literal[1] = 1
    budget_quote: InputNumber = Field(gt=0)
    fee_reserve_quote: InputNumber
    base_quote: InputNumber = Field(gt=0)
    addition_quote: InputNumber = Field(gt=0)
    max_additions: int = Field(strict=True, ge=0, le=64)
    deviation_pct: InputNumber = Field(gt=0, lt=100)
    take_profit_pct: InputNumber = Field(gt=0, le=1000)
    stop_loss_pct: InputNumber = Field(gt=0, lt=100)

    @model_validator(mode="after")
    def bounded_ladder(self):
        if self.max_additions * self.deviation_pct >= self.stop_loss_pct:
            raise ValueError("DCA additions must remain above the basket stop")
        if (
            self.base_quote + self.max_additions * self.addition_quote + self.fee_reserve_quote
            > self.budget_quote
        ):
            raise ValueError("DCA cash budget cannot fund all additions")
        return self


class SpotGridParams(_SpotParams):
    """Fixed arithmetic buy-first grid, with finite slot count and allocation."""

    version: Literal[1] = 1
    budget_quote: InputNumber = Field(gt=0)
    fee_reserve_quote: InputNumber
    lower_price: InputNumber = Field(gt=0)
    upper_price: InputNumber = Field(gt=0)
    intervals: int = Field(strict=True, ge=1, le=64)
    quantity_per_slot: InputNumber = Field(gt=0)
    stop_price: InputNumber = Field(gt=0)

    @model_validator(mode="after")
    def bounded_range(self):
        if not self.stop_price < self.lower_price < self.upper_price:
            raise ValueError("Grid requires stop < lower < upper")
        if self.fee_reserve_quote >= self.budget_quote:
            raise ValueError("Grid fee reserve exhausts cash budget")
        return self


# ---------------------------------------------------------------------------
# Signal blocks
# ---------------------------------------------------------------------------


class BosParams(_StrictModel):
    """Break of Structure — `koval.strategy.helpers.signals.smc.detect_bos`."""

    lookback: int = Field(10, ge=2, le=200)


class ChochParams(_StrictModel):
    """Change of Character — `detect_choch`."""

    lookback: int = Field(5, ge=2, le=200)


class FvgParams(_StrictModel):
    """Fair Value Gap — `detect_fvg` takes no params."""


class ObParams(_StrictModel):
    """Order Block — `detect_ob`."""

    lookback: int = Field(10, ge=2, le=200)


class HammerParams(_StrictModel):
    """Hammer candle — `is_hammer`."""

    body_ratio: float = Field(2.0, gt=0)


class ShootingStarParams(_StrictModel):
    """Shooting Star — `is_shooting_star`."""

    body_ratio: float = Field(2.0, gt=0)


class BullishEngulfingParams(_StrictModel):
    """Bullish Engulfing — `is_bullish_engulfing` takes no params."""


class BearishEngulfingParams(_StrictModel):
    """Bearish Engulfing — `is_bearish_engulfing` takes no params."""


class EveryBarSignalParams(_StrictModel):
    """Diagnostic paper-test signal that emits on every closed bar."""

    direction: Literal["bullish", "bearish"] = "bullish"


class EmaCrossParams(_StrictModel):
    """EMA cross signal — `ema_cross`."""

    fast: int = Field(9, ge=2)
    slow: int = Field(21, ge=3)

    @model_validator(mode="after")
    def _fast_lt_slow(self) -> EmaCrossParams:
        if self.fast >= self.slow:
            raise ValueError(f"fast ({self.fast}) must be < slow ({self.slow})")
        return self


class RsiCrossParams(_StrictModel):
    """RSI cross signal — `rsi_cross`."""

    period: int = Field(14, ge=2, le=200)
    level: float = Field(50.0, ge=0, le=100)
    direction: Literal["cross_up", "cross_down"] = "cross_up"


# ---------------------------------------------------------------------------
# Filter blocks
# ---------------------------------------------------------------------------


class RsiFilterParams(_StrictModel):
    """RSI band filter — `rsi_filter`."""

    period: int = Field(14, ge=2)
    min_val: float = Field(30.0, ge=0, le=100)
    max_val: float = Field(70.0, ge=0, le=100)

    @model_validator(mode="after")
    def _min_le_max(self) -> RsiFilterParams:
        if self.min_val > self.max_val:
            raise ValueError(f"min_val ({self.min_val}) must be <= max_val ({self.max_val})")
        return self


class StochFilterParams(_StrictModel):
    """Stochastic %K band filter — `stoch_filter`."""

    k_period: int = Field(14, ge=2)
    min_val: float = Field(20.0, ge=0, le=100)
    max_val: float = Field(80.0, ge=0, le=100)

    @model_validator(mode="after")
    def _min_le_max(self) -> StochFilterParams:
        if self.min_val > self.max_val:
            raise ValueError(f"min_val ({self.min_val}) must be <= max_val ({self.max_val})")
        return self


class MacdFilterParams(_StrictModel):
    """MACD histogram-sign filter — `macd_filter`."""

    fast: int = Field(12, ge=2)
    slow: int = Field(26, ge=3)
    signal_period: int = Field(9, ge=2)
    require_positive: bool = True

    @model_validator(mode="after")
    def _fast_lt_slow(self) -> MacdFilterParams:
        if self.fast >= self.slow:
            raise ValueError(f"fast ({self.fast}) must be < slow ({self.slow})")
        return self


class AdxFilterParams(_StrictModel):
    """ADX strength filter — `adx_filter`."""

    period: int = Field(14, ge=2)
    min_adx: float = Field(25.0, ge=0, le=100)


class EmaTrendFilterParams(_StrictModel):
    """EMA trend filter — `ema_trend_filter`."""

    period: int = Field(200, ge=2)
    direction: Literal["bullish", "bearish"] = "bullish"


class AtrVolatilityParams(_StrictModel):
    """ATR-percent volatility filter — `atr_volatility_filter`."""

    period: int = Field(14, ge=2)
    min_atr_pct: float = Field(0.5, ge=0)
    max_atr_pct: float | None = Field(None, gt=0)

    @model_validator(mode="after")
    def _max_above_min(self) -> AtrVolatilityParams:
        if self.max_atr_pct is not None and self.max_atr_pct <= self.min_atr_pct:
            raise ValueError(
                f"max_atr_pct ({self.max_atr_pct}) must be > min_atr_pct ({self.min_atr_pct})"
            )
        return self

    @model_serializer(mode="wrap")
    def _omit_unset_cap(self, handler):
        # Hosts store and hash these dumps; an unset cap must keep the pre-cap shape.
        data = handler(self)
        if data.get("max_atr_pct") is None:
            data.pop("max_atr_pct", None)
        return data


class CooldownParams(_StrictModel):
    """Post-exit entry cooldown, counted in flat bars — `policy.cooldown`."""

    bars: int = Field(..., ge=1)


class BbVolatilityParams(_StrictModel):
    """Bollinger Bandwidth volatility filter — `bb_volatility_filter`."""

    period: int = Field(20, ge=2)
    std_dev: float = Field(2.0, gt=0)
    min_bandwidth_pct: float = Field(1.0, ge=0)


class IsDojiFilterParams(_StrictModel):
    """Doji-bar filter — `is_doji` evaluated on the current bar."""

    body_pct: float = Field(0.1, gt=0, le=1)


# ---------------------------------------------------------------------------
# Entry block
# ---------------------------------------------------------------------------


class EntryParams(_StrictModel):
    """Shared params for `entry.long_only` / `entry.short_only` / `entry.both`."""

    entry_type: Literal["market", "limit", "stop"] = "market"


# ---------------------------------------------------------------------------
# Exit blocks (initial bracket + dynamic)
# ---------------------------------------------------------------------------


class FixedSlTpParams(_StrictModel):
    """Fixed SL/TP — `fixed_sl_tp`. `tp_pct` overrides `risk_reward` when set."""

    sl_pct: float = Field(2.0, gt=0)
    risk_reward: float = Field(2.0, gt=0)
    tp_pct: float | None = Field(None, gt=0)


class TrailingStopParams(_StrictModel):
    """Trailing-stop dynamic exit — `trailing_stop_price`."""

    trail_pct: float = Field(2.0, gt=0)


class BreakevenParams(_StrictModel):
    """Move-to-breakeven dynamic exit — `breakeven_trigger`."""

    trigger_r: float = Field(1.0, gt=0)


# ---------------------------------------------------------------------------
# Risk block
# ---------------------------------------------------------------------------


class PctRiskParams(_StrictModel):
    """Fixed-percent risk position sizer — `calculate_position_size`."""

    risk_pct: float = Field(1.0, gt=0, le=100)
    leverage: float = Field(1.0, gt=0)
