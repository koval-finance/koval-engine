from __future__ import annotations

import inspect
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from koval.exchanges.whitebit import WhiteBITAdapter


def test_base_url_constant():
    a = WhiteBITAdapter()
    assert a.base_url == "https://whitebit.com"


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
        WhiteBITAdapter(**kwargs)


def test_read_only_adapter_does_not_accept_or_store_credentials():
    parameters = inspect.signature(WhiteBITAdapter).parameters

    assert "api_key" not in parameters
    assert "api_secret" not in parameters
    assert "session" in parameters


def test_symbol_normalization():
    a = WhiteBITAdapter()
    assert a._normalize_symbol("BTC/USDT") == "BTC_USDT"
    assert a._normalize_symbol("BTC_USDT") == "BTC_USDT"


def test_interval_mapping_known():
    a = WhiteBITAdapter()
    assert a._interval("1m") == "1m"
    assert a._interval("1h") == "1h"
    assert a._interval("1d") == "1d"


def test_interval_mapping_unknown_raises():
    a = WhiteBITAdapter()
    with pytest.raises(ValueError):
        a._interval("2h")


KLINE_URL = "https://whitebit.com/api/v1/public/kline"


def _kline_api_body(rows: list) -> dict:
    return {"success": True, "message": None, "result": rows}


def _whitebit_kline(open_seconds: int, idx: int) -> list:
    # WhiteBIT order: [time_s, open, close, high, low, volume, quote_volume]
    return [
        open_seconds,
        f"{100 + idx}.0",
        f"{100 + idx + 0.5}",
        f"{101 + idx}.0",
        f"{99 + idx}.0",
        f"{10 + idx}.0",
        "0.0",
    ]


@responses.activate
def test_fetch_ohlcv_basic_and_column_remap():
    body = [_whitebit_kline(1_700_000_000 + i * 60, i) for i in range(3)]
    responses.add(responses.GET, KLINE_URL, json=_kline_api_body(body), status=200)

    a = WhiteBITAdapter()
    out = a.fetch_ohlcv("BTC/USDT", "1m", 1_700_000_000_000, 1_700_000_180_000)

    assert out.shape == (3, 6)
    assert out[0, 0] == 1_700_000_000_000  # converted s -> ms
    assert out[0, 1] == 100.0  # open
    assert out[0, 2] == 101.0  # high (column 3 in source becomes col 2 here)
    assert out[0, 3] == 99.0  # low
    assert out[0, 4] == 100.5  # close
    assert out[0, 5] == 10.0  # volume

    call = responses.calls[0].request
    assert "market=BTC_USDT" in call.url
    assert "interval=1m" in call.url
    assert "start=1700000000" in call.url
    assert "end=1700000180" in call.url
    assert "limit=1440" in call.url


@responses.activate
def test_fetch_ohlcv_enforces_range_order_and_uniqueness():
    start_s = 1_700_000_000
    body = [
        _whitebit_kline(start_s + 120, 2),
        _whitebit_kline(start_s, 0),
        _whitebit_kline(start_s + 60, 1),
        _whitebit_kline(start_s + 60, 99),
        _whitebit_kline(start_s + 180, 3),
    ]
    responses.add(responses.GET, KLINE_URL, json=_kline_api_body(body), status=200)

    out = WhiteBITAdapter().fetch_ohlcv(
        "BTC/USDT",
        "1m",
        start_s * 1000,
        (start_s + 180) * 1000,
    )

    assert [int(timestamp) for timestamp in out[:, 0]] == [
        start_s * 1000,
        (start_s + 60) * 1000,
        (start_s + 120) * 1000,
    ]


@responses.activate
def test_fetch_ohlcv_raises_when_api_returns_success_false():
    responses.add(
        responses.GET,
        KLINE_URL,
        json={
            "success": False,
            "message": {"market": ["Market is not available."]},
            "result": None,
        },
        status=200,
    )
    a = WhiteBITAdapter()
    with pytest.raises(requests.HTTPError, match="Market is not available"):
        a.fetch_ohlcv("INVALID", "1h", 1_700_000_000_000, 1_700_000_060_000)


@responses.activate
def test_fetch_ohlcv_empty_response():
    responses.add(responses.GET, KLINE_URL, json=_kline_api_body([]), status=200)
    a = WhiteBITAdapter()
    out = a.fetch_ohlcv("BTC/USDT", "1m", 1_700_000_000_000, 1_700_000_060_000)
    assert out.shape == (0, 6)


@responses.activate
def test_fetch_ohlcv_retries_rate_limit_response():
    start_s = 1_700_000_000
    responses.add(responses.GET, KLINE_URL, status=429, headers={"Retry-After": "0"})
    responses.add(
        responses.GET,
        KLINE_URL,
        json=_kline_api_body([_whitebit_kline(start_s, 0)]),
        status=200,
    )

    out = WhiteBITAdapter(retry_backoff_seconds=0.0).fetch_ohlcv(
        "BTC/USDT",
        "1m",
        start_s * 1000,
        (start_s + 60) * 1000,
    )

    assert out.shape == (1, 6)
    assert len(responses.calls) == 2


@responses.activate
def test_fetch_ohlcv_paginates_across_limit():
    page_a = [_whitebit_kline(1_700_000_000 + i * 60, i) for i in range(2)]
    page_b = [_whitebit_kline(1_700_000_120 + i * 60, i + 2) for i in range(2)]
    page_c: list = []

    responses.add(responses.GET, KLINE_URL, json=_kline_api_body(page_a), status=200)
    responses.add(responses.GET, KLINE_URL, json=_kline_api_body(page_b), status=200)
    responses.add(responses.GET, KLINE_URL, json=_kline_api_body(page_c), status=200)

    a = WhiteBITAdapter(page_limit=2)
    out = a.fetch_ohlcv("BTC/USDT", "1m", 1_700_000_000_000, 1_700_000_300_000)
    assert out.shape == (4, 6)
    assert out[0, 0] == 1_700_000_000_000
    assert out[-1, 0] == 1_700_000_180_000
    assert len(responses.calls) == 3


@responses.activate
def test_fetch_ohlcv_rejects_non_advancing_pagination():
    start_s = 1_700_000_000
    repeated = [_whitebit_kline(start_s + i * 60, i) for i in range(2)]
    responses.add(responses.GET, KLINE_URL, json=_kline_api_body(repeated), status=200)
    responses.add(responses.GET, KLINE_URL, json=_kline_api_body(repeated), status=200)
    responses.add(responses.GET, KLINE_URL, json=_kline_api_body([]), status=200)

    with pytest.raises(RuntimeError, match="pagination did not advance"):
        WhiteBITAdapter(page_limit=2).fetch_ohlcv(
            "BTC/USDT",
            "1m",
            start_s * 1000,
            (start_s + 300) * 1000,
        )


def test_future_market_symbol_translation():
    a = WhiteBITAdapter(market="future")
    assert a._market_symbol("BTC_USDT") == "BTC_PERP"
    assert a._market_symbol("BTC/USDT") == "BTC_PERP"
    assert a._market_symbol("BTCUSDT") == "BTC_PERP"
    assert a._market_symbol("BTC_PERP") == "BTC_PERP"
    assert a._market_symbol("1000SHIBUSDT") == "1000SHIB_PERP"


def test_future_market_symbol_rejects_unparseable_pair():
    a = WhiteBITAdapter(market="future")
    with pytest.raises(ValueError):
        a._market_symbol("BTC")


def test_spot_market_symbol_is_left_untranslated():
    a = WhiteBITAdapter(market="spot")
    assert a._market_symbol("BTCUSDT") == "BTCUSDT"
    assert a._market_symbol("BTC/USDT") == "BTC_USDT"


@responses.activate
def test_fetch_funding_history_normalizes_whitebit_settlement_evidence():
    interval_s = 8 * 60 * 60
    first_settlement_s = interval_s
    second_settlement_s = 2 * interval_s
    responses.add(
        responses.GET,
        "https://whitebit.com/api/v4/public/funding-history/BTC_PERP",
        json=[
            {
                "fundingTime": str(first_settlement_s),
                "fundingRate": "0.0001",
                "market": "BTC_PERP",
                "settlementPrice": "100.5",
                "rateCalculatedTime": "0",
            },
            {
                "fundingTime": str(second_settlement_s),
                "fundingRate": "-0.0002",
                "market": "BTC_PERP",
                "settlementPrice": "101.5",
                "rateCalculatedTime": str(first_settlement_s),
            },
        ],
    )
    adapter = WhiteBITAdapter(market="future")

    series = adapter.fetch_funding_history(
        "BTCUSDT", first_settlement_s * 1000, second_settlement_s * 1000
    )

    assert series.canonical_symbol == "BTCUSDT"
    assert series.records[0].settlement_timestamp_ms == first_settlement_s * 1000
    assert series.records[0].rate_calculated_timestamp_ms == 0
    assert str(series.records[1].settlement_mark_price) == "101.5"
    assert series.coverage_complete
    query = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert query["limit"] == ["100"]
