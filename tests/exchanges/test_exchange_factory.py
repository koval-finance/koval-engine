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
