import inspect
import math

import pytest

from koval.engine.execution_settings import (
    SUPPORTED_EXECUTION_MODES,
    apply_execution_settings,
    normalize_execution_mode,
    resolve_execution_settings,
)


def test_resolve_execution_settings_uses_binance_spot_defaults():
    settings = resolve_execution_settings(
        {
            "exchange": "binance",
            "exchange_type": "spot",
            "execution_mode": "paper",
        }
    )

    assert settings.maker_fee_bps == 10.0
    assert settings.taker_fee_bps == 10.0
    assert settings.broker_commission_bps == 10.0
    assert settings.commission_rate == 0.001
    assert settings.fee_source == "exchange_default"


def test_resolve_execution_settings_converts_legacy_taker_fee_percent():
    settings = resolve_execution_settings(
        {
            "exchange": "binance",
            "exchange_type": "future",
            "taker_fee": 0.04,
        }
    )

    assert settings.taker_fee_bps == 4.0
    assert settings.broker_commission_bps == 4.0
    assert settings.commission_rate == 0.0004
    assert settings.fee_source == "legacy_taker_fee"


def test_apply_execution_settings_merges_fields_into_config():
    config = apply_execution_settings({"exchange": "binance", "exchange_type": "future"})

    assert config["execution_mode"] == "paper"
    assert config["maker_fee_bps"] == 2.0
    assert config["taker_fee_bps"] == 4.0
    assert config["commission"] == 0.0004
    assert config["paper_commission_side"] == "taker"


def test_exchange_default_fee_source_does_not_lock_old_derived_values():
    config = apply_execution_settings(
        {
            "exchange": "binance",
            "exchange_type": "spot",
            "fee_source": "exchange_default",
            "maker_fee_bps": 2.0,
            "taker_fee_bps": 4.0,
            "commission": 0.0004,
        }
    )

    assert config["maker_fee_bps"] == 10.0
    assert config["taker_fee_bps"] == 10.0
    assert config["commission"] == 0.001


def test_execution_mode_scope_matches_reachable_broker_modes():
    assert SUPPORTED_EXECUTION_MODES == {"paper", "binance_sandbox"}
    assert normalize_execution_mode("binance_sandbox") == "binance_sandbox"
    assert normalize_execution_mode("sandbox") == "paper"
    assert normalize_execution_mode("real") == "paper"
    assert normalize_execution_mode("") == "paper"


def test_real_mode_fails_closed_to_paper_defaults():
    settings = resolve_execution_settings(
        {
            "exchange": "binance",
            "exchange_type": "future",
            "execution_mode": "real",
            "symbol": "BTC/USDT",
        }
    )

    assert settings.execution_mode == "paper"
    assert settings.fee_source == "exchange_default"


def test_execution_settings_api_has_no_unused_account_fee_provider_hook():
    assert "commission_provider" not in inspect.signature(resolve_execution_settings).parameters
    assert "commission_provider" not in inspect.signature(apply_execution_settings).parameters


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("maker_fee_bps", -1),
        ("maker_fee_bps", float("nan")),
        ("maker_fee_bps", float("inf")),
        ("maker_fee_bps", True),
        ("taker_fee_bps", -1),
        ("taker_fee", -0.01),
        ("commission", -0.01),
    ],
)
def test_invalid_fee_values_fall_back_to_finite_non_negative_exchange_defaults(field, value):
    settings = resolve_execution_settings(
        {
            "exchange": "binance",
            "exchange_type": "future",
            field: value,
        }
    )

    assert settings.maker_fee_bps == 2.0
    assert settings.taker_fee_bps == 4.0
    assert math.isfinite(settings.commission_rate)
    assert settings.commission_rate >= 0
    assert settings.fee_source == "exchange_default"


def test_unknown_fee_schedule_fails_instead_of_zero():
    from koval.engine.execution_settings import resolve_execution_settings

    with pytest.raises(ValueError, match="no default fee schedule"):
        resolve_execution_settings({"exchange": "whitebit", "exchange_type": "future"})
    settings = resolve_execution_settings(
        {
            "exchange": "whitebit",
            "exchange_type": "future",
            "maker_fee_bps": 1.0,
            "taker_fee_bps": 3.5,
        }
    )
    assert settings.taker_fee_bps == 3.5 and settings.fee_source == "config_override"


def test_unrecognized_market_string_is_rejected_like_canonical_market():
    """Execution settings must not be laxer than ``markets.canonical_market``:
    an unrecognized market string fails instead of silently resolving to spot."""
    from koval.exchanges.markets import canonical_market

    with pytest.raises(ValueError):
        canonical_market("swap")
    with pytest.raises(ValueError):
        resolve_execution_settings({"exchange": "binance", "exchange_type": "swap"})


def test_whitebit_spot_has_a_default_fee_schedule():
    settings = resolve_execution_settings({"exchange": "whitebit", "exchange_type": "spot"})

    assert settings.maker_fee_bps == 10.0
    assert settings.taker_fee_bps == 10.0
    assert settings.fee_source == "exchange_default"
