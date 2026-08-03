from __future__ import annotations

import inspect

import numpy as np
import pytest
import requests
import responses

from koval.exchanges.binance import BinanceAdapter


def test_testnet_url_selected():
    adapter = BinanceAdapter(testnet=True)
    assert adapter.base_url == "https://testnet.binancefuture.com"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"page_limit": 1.5},
        {"page_limit": True},
        {"max_retries": 1.5},
        {"max_retries": True},
        {"retry_backoff_seconds": float("nan")},
        {"retry_backoff_seconds": float("inf")},
    ],
)
def test_constructor_rejects_non_finite_or_wrongly_typed_transport_settings(kwargs):
    with pytest.raises(ValueError, match="transport settings"):
        BinanceAdapter(**kwargs)


def test_read_only_adapter_does_not_accept_or_store_credentials():
    parameters = inspect.signature(BinanceAdapter).parameters

    assert "api_key" not in parameters
    assert "api_secret" not in parameters
    assert "session" in parameters


def test_prod_url_selected():
    adapter = BinanceAdapter(testnet=False)
    assert adapter.base_url == "https://fapi.binance.com"


def test_symbol_normalization():
    adapter = BinanceAdapter(testnet=True)
    assert adapter._normalize_symbol("BTC/USDT") == "BTCUSDT"
    assert adapter._normalize_symbol("BTCUSDT") == "BTCUSDT"
    assert adapter._normalize_symbol("btc/usdt") == "BTCUSDT"


def test_interval_mapping():
    adapter = BinanceAdapter(testnet=True)
    assert adapter._interval("1m") == "1m"
    assert adapter._interval("1h") == "1h"
    assert adapter._interval("1d") == "1d"
    with pytest.raises(ValueError, match="unknown timeframe"):
        adapter._interval("2h")


KLINES_URL = "https://testnet.binancefuture.com/fapi/v1/klines"


def _binance_kline(open_ms: int, idx: int) -> list:
    return [
        open_ms,
        f"{100 + idx}.0",
        f"{101 + idx}.0",
        f"{99 + idx}.0",
        f"{100 + idx + 0.5}",
        f"{10 + idx}.0",
        open_ms + 60_000 - 1,
        "0",
        0,
        "0",
        "0",
        "0",
    ]


@responses.activate
def test_fetch_ohlcv_basic():
    body = [_binance_kline(1_700_000_000_000 + i * 60_000, i) for i in range(3)]
    responses.add(responses.GET, KLINES_URL, json=body, status=200)

    a = BinanceAdapter(testnet=True)
    out = a.fetch_ohlcv("BTC/USDT", "1m", 1_700_000_000_000, 1_700_000_180_000)

    assert out.shape == (3, 6)
    assert out.dtype == np.float64
    assert out[0, 0] == 1_700_000_000_000
    assert out[0, 1] == 100.0
    assert out[2, 4] == 102.5

    assert len(responses.calls) == 1
    call = responses.calls[0].request
    assert "symbol=BTCUSDT" in call.url
    assert "interval=1m" in call.url
    assert "startTime=1700000000000" in call.url
    assert "endTime=1700000180000" in call.url
    assert "limit=1500" in call.url


@responses.activate
def test_fetch_ohlcv_enforces_range_order_and_uniqueness():
    start = 1_700_000_000_000
    body = [
        _binance_kline(start + 120_000, 2),
        _binance_kline(start, 0),
        _binance_kline(start + 60_000, 1),
        _binance_kline(start + 60_000, 99),
        _binance_kline(start + 180_000, 3),
    ]
    responses.add(responses.GET, KLINES_URL, json=body, status=200)

    out = BinanceAdapter(testnet=True).fetch_ohlcv("BTC/USDT", "1m", start, start + 180_000)

    np.testing.assert_array_equal(out[:, 0], [start, start + 60_000, start + 120_000])


@responses.activate
def test_fetch_ohlcv_empty_response():
    responses.add(responses.GET, KLINES_URL, json=[], status=200)
    a = BinanceAdapter(testnet=True)
    out = a.fetch_ohlcv("BTC/USDT", "1m", 1_700_000_000_000, 1_700_000_060_000)
    assert out.shape == (0, 6)


@responses.activate
def test_fetch_ohlcv_retries_transient_server_failure():
    start = 1_700_000_000_000
    responses.add(responses.GET, KLINES_URL, status=503)
    responses.add(
        responses.GET,
        KLINES_URL,
        json=[_binance_kline(start, 0)],
        status=200,
    )

    out = BinanceAdapter(testnet=True, retry_backoff_seconds=0.0).fetch_ohlcv(
        "BTC/USDT",
        "1m",
        start,
        start + 60_000,
    )

    assert out.shape == (1, 6)
    assert len(responses.calls) == 2


@responses.activate
def test_fetch_ohlcv_retries_transient_transport_failure():
    start = 1_700_000_000_000
    responses.add(
        responses.GET,
        KLINES_URL,
        body=requests.Timeout("temporary timeout"),
    )
    responses.add(
        responses.GET,
        KLINES_URL,
        json=[_binance_kline(start, 0)],
        status=200,
    )

    out = BinanceAdapter(testnet=True, retry_backoff_seconds=0.0).fetch_ohlcv(
        "BTC/USDT",
        "1m",
        start,
        start + 60_000,
    )

    assert out.shape == (1, 6)
    assert len(responses.calls) == 2


@responses.activate
def test_fetch_ohlcv_stops_after_transport_retry_budget_is_exhausted():
    for _ in range(3):
        responses.add(
            responses.GET,
            KLINES_URL,
            body=requests.ConnectionError("network unavailable"),
        )

    with pytest.raises(requests.ConnectionError, match="network unavailable"):
        BinanceAdapter(
            testnet=True,
            max_retries=2,
            retry_backoff_seconds=0.0,
        ).fetch_ohlcv(
            "BTC/USDT",
            "1m",
            1_700_000_000_000,
            1_700_000_060_000,
        )

    assert len(responses.calls) == 3


@responses.activate
def test_fetch_ohlcv_paginates_when_range_exceeds_limit():
    # Make the per-page limit tiny to keep the test small.
    page_a = [_binance_kline(1_700_000_000_000 + i * 60_000, i) for i in range(2)]
    page_b = [_binance_kline(1_700_000_120_000 + i * 60_000, i + 2) for i in range(2)]
    page_c: list = []

    responses.add(responses.GET, KLINES_URL, json=page_a, status=200)
    responses.add(responses.GET, KLINES_URL, json=page_b, status=200)
    responses.add(responses.GET, KLINES_URL, json=page_c, status=200)

    a = BinanceAdapter(testnet=True, page_limit=2)
    out = a.fetch_ohlcv("BTC/USDT", "1m", 1_700_000_000_000, 1_700_000_300_000)

    assert out.shape == (4, 6)
    assert out[0, 0] == 1_700_000_000_000
    assert out[-1, 0] == 1_700_000_180_000
    assert len(responses.calls) == 3


@responses.activate
def test_fetch_ohlcv_rejects_non_advancing_pagination():
    start = 1_700_000_000_000
    repeated = [_binance_kline(start + i * 60_000, i) for i in range(2)]
    responses.add(responses.GET, KLINES_URL, json=repeated, status=200)
    responses.add(responses.GET, KLINES_URL, json=repeated, status=200)
    responses.add(responses.GET, KLINES_URL, json=[], status=200)

    with pytest.raises(RuntimeError, match="pagination did not advance"):
        BinanceAdapter(testnet=True, page_limit=2).fetch_ohlcv(
            "BTC/USDT", "1m", start, start + 300_000
        )
