"""Tests for the exchange-adapter factory."""

import pytest

from koval.exchanges import get_exchange_adapter, is_supported_exchange
from koval.exchanges.binance import BinanceAdapter
from koval.exchanges.whitebit import WhiteBITAdapter


def test_is_supported_exchange():
    assert is_supported_exchange("binance") is True
    assert is_supported_exchange("WhiteBIT") is True
    assert is_supported_exchange("kraken") is False


def test_get_exchange_adapter_binance():
    assert isinstance(get_exchange_adapter("binance"), BinanceAdapter)


def test_get_exchange_adapter_whitebit():
    assert isinstance(get_exchange_adapter("whitebit"), WhiteBITAdapter)


def test_get_exchange_adapter_rejects_unknown():
    with pytest.raises(ValueError):
        get_exchange_adapter("kraken")


def test_default_market_preserves_historical_data_source():
    assert get_exchange_adapter("binance").capabilities().market == "future"
    assert get_exchange_adapter("whitebit").capabilities().market == "spot"


def test_explicit_markets_route_to_their_sources():
    spot = get_exchange_adapter("binance", exchange_type="spot")
    assert spot.capabilities().market == "spot"
    assert spot.base_url == "https://api.binance.com"
    perp = get_exchange_adapter("whitebit", exchange_type="future")
    assert perp.capabilities().market == "future"


def test_unsupported_pair_fails():
    with pytest.raises(ValueError, match="unsupported"):
        get_exchange_adapter("binance", exchange_type="margin")


def test_capabilities_declare_the_data_environment_and_symbol_format():
    binance = get_exchange_adapter("binance").capabilities()
    assert binance.exchange == "binance"
    assert binance.data_environment == "production"
    assert binance.sandbox_execution is True
    assert binance.symbol_format == "BTCUSDT"
    whitebit = get_exchange_adapter("whitebit").capabilities()
    assert whitebit.sandbox_execution is False
    assert whitebit.symbol_format == "BTC_USDT"
    assert get_exchange_adapter(
        "whitebit", exchange_type="future"
    ).capabilities().symbol_format == ("BTC_PERP")


def test_adapter_base_contract_marks_unimplemented_funding_as_unavailable(
    fake_adapter_factory,
):
    from koval.engine.funding import FundingUnavailableError

    with pytest.raises(FundingUnavailableError, match="unavailable"):
        fake_adapter_factory([]).fetch_funding_history("BTCUSDT", 0, 1)
