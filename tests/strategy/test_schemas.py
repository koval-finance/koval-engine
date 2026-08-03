from __future__ import annotations

import pytest
from pydantic import ValidationError

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


def test_ema_cross_defaults():
    p = EmaCrossParams()
    assert p.fast == 9
    assert p.slow == 21


def test_ema_cross_rejects_fast_geq_slow():
    with pytest.raises(ValidationError):
        EmaCrossParams(fast=21, slow=9)


def test_rsi_filter_bounds_validation():
    with pytest.raises(ValidationError):
        RsiFilterParams(min_val=70, max_val=30)


def test_pct_risk_positive():
    with pytest.raises(ValidationError):
        PctRiskParams(risk_pct=0)
    with pytest.raises(ValidationError):
        PctRiskParams(leverage=0)


def test_fixed_sl_tp_either_rr_or_tp_pct():
    p = FixedSlTpParams(sl_pct=2.0, risk_reward=2.0)
    assert p.tp_pct is None
    p2 = FixedSlTpParams(sl_pct=2.0, tp_pct=5.0)
    assert p2.tp_pct == 5.0


def test_entry_params_default_market():
    p = EntryParams()
    assert p.entry_type == "market"


def test_ema_trend_filter_direction_enum():
    with pytest.raises(ValidationError):
        EmaTrendFilterParams(direction="sideways")


def test_rsi_cross_direction_enum():
    p = RsiCrossParams(direction="cross_up", level=30)
    assert p.direction == "cross_up"


def test_macd_filter_rejects_fast_geq_slow():
    with pytest.raises(ValidationError):
        MacdFilterParams(fast=26, slow=12)


def test_extra_keys_forbidden():
    """Every schema rejects unknown keys (UI typos must fail loudly)."""
    with pytest.raises(ValidationError):
        EmaCrossParams(fast=9, slow=21, unknown_key=1)
    with pytest.raises(ValidationError):
        FvgParams(unexpected=True)
    with pytest.raises(ValidationError):
        EntryParams(entry_type="market", extra="x")


def test_all_models_emit_json_schema():
    """Every param model must expose model_json_schema() — required for dashboard."""
    models = [
        BosParams,
        ChochParams,
        FvgParams,
        ObParams,
        HammerParams,
        ShootingStarParams,
        BullishEngulfingParams,
        BearishEngulfingParams,
        EmaCrossParams,
        RsiCrossParams,
        RsiFilterParams,
        StochFilterParams,
        MacdFilterParams,
        AdxFilterParams,
        EmaTrendFilterParams,
        AtrVolatilityParams,
        BbVolatilityParams,
        IsDojiFilterParams,
        EntryParams,
        FixedSlTpParams,
        TrailingStopParams,
        BreakevenParams,
        PctRiskParams,
    ]
    for m in models:
        schema = m.model_json_schema()
        assert "properties" in schema or schema.get("type") == "object"
