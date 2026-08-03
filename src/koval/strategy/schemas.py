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

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
