import pytest

from koval.engine.paper_profile import (
    PAPER_FIXED_VERSION,
    PAPER_LEGACY_VERSION,
    PAPER_REALISTIC_VERSION,
    resolve_paper_profile,
)

FIXED = {
    "version": PAPER_FIXED_VERSION,
    "commission_bps": 4.0,
    "spread_bps": 20.0,
    "slippage_bps": 10.0,
}


def test_empty_config_is_legacy():
    profile = resolve_paper_profile(None)
    assert profile.version == PAPER_LEGACY_VERSION and not profile.is_fixed
    assert resolve_paper_profile({}).version == PAPER_LEGACY_VERSION


def test_fixed_profile_resolves_costs_and_default_leverage():
    profile = resolve_paper_profile(FIXED)
    assert profile.is_fixed and profile.leverage == 1.0
    assert profile.adjustment_fraction == pytest.approx(0.002)
    assert profile.as_config() == {**FIXED, "leverage": 1.0}


def test_realistic_profile_versions_same_bar_conservative_protection():
    profile = resolve_paper_profile({**FIXED, "version": PAPER_REALISTIC_VERSION})

    assert profile.is_costed
    assert not profile.delays_protection
    assert profile.ambiguity_policy == "conservative_stop_first"
    assert profile.as_config()["equal_timestamp_order"] == [
        "funding_settlement",
        "mark_price_liquidation",
        "entry_fill",
        "bracket_activation",
        "stop_or_target_fill",
        "oco_sibling_cancel",
        "dynamic_replacement",
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"version": "paper_v9"},
        {**FIXED, "funding_bps": 1.0},
        {k: v for k, v in FIXED.items() if k != "slippage_bps"},
        {**FIXED, "commission_bps": "4"},
        {**FIXED, "spread_bps": True},
        {**FIXED, "spread_bps": 19_990.0, "slippage_bps": 10.0},
        {**FIXED, "leverage": 0.5},
        {**FIXED, "leverage": 126},
        {"version": PAPER_LEGACY_VERSION, "commission_bps": 4.0},
    ],
)
def test_invalid_profiles_fail(bad):
    with pytest.raises(ValueError):
        resolve_paper_profile(bad)
