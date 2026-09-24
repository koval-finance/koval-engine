from __future__ import annotations

import inspect
from decimal import Decimal

import numpy as np
import pytest
import requests
import responses

from koval.exchanges.binance import BinanceAdapter
from koval.exchanges.binance_evidence import BinanceFuturesEvidenceClient


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


@responses.activate
def test_fetch_funding_history_normalizes_settlement_evidence():
    interval = 8 * 60 * 60 * 1000
    responses.add(
        responses.GET,
        "https://fapi.binance.com/fapi/v1/fundingRate",
        json=[
            {
                "symbol": "BTCUSDT",
                "fundingTime": 0,
                "fundingRate": "0.00010000",
                "markPrice": "100.5",
            },
            {
                "symbol": "BTCUSDT",
                "fundingTime": interval,
                "fundingRate": "-0.00020000",
                "markPrice": "101.5",
            },
        ],
    )
    adapter = BinanceAdapter(testnet=False, market="future")

    series = adapter.fetch_funding_history("BTC/USDT", 0, interval)

    assert [str(record.rate) for record in series.records] == ["0.00010000", "-0.00020000"]
    assert [str(record.settlement_mark_price) for record in series.records] == [
        "100.5",
        "101.5",
    ]
    assert series.coverage_complete
    assert series.raw_responses


@responses.activate
def test_binance_funding_does_not_invent_an_interval_from_one_record():
    responses.add(
        responses.GET,
        "https://fapi.binance.com/fapi/v1/fundingRate",
        json=[
            {
                "symbol": "BTCUSDT",
                "fundingTime": 28_800_000,
                "fundingRate": "0.00010000",
                "markPrice": "100.5",
            }
        ],
    )

    with pytest.raises(ValueError, match="funding interval is unavailable"):
        BinanceAdapter(testnet=False, market="future").fetch_funding_history(
            "BTCUSDT", 1, 28_800_001
        )


@responses.activate
def test_binance_funding_proves_an_empty_short_window_from_prior_settlements():
    interval = 8 * 60 * 60 * 1000
    responses.add(
        responses.GET,
        "https://fapi.binance.com/fapi/v1/fundingRate",
        json=[
            {
                "symbol": "BTCUSDT",
                "fundingTime": index * interval,
                "fundingRate": "0.00010000",
                "markPrice": "100.5",
            }
            for index in (1, 2, 3)
        ],
    )

    series = BinanceAdapter(testnet=False, market="future").fetch_funding_history(
        "BTCUSDT", 3 * interval + 1, 4 * interval - 1
    )

    assert series.records == ()
    assert series.interval_ms == interval
    assert series.settlement_anchor_ms == 3 * interval
    assert series.schedule_source == "binance_usdm_funding_rate_history"
    query = responses.calls[0].request.url
    assert "startTime=1" not in query
    assert "startTime=86400001" not in query


def test_binance_spot_funding_is_explicitly_unavailable():
    from koval.engine.funding import FundingUnavailableError

    with pytest.raises(FundingUnavailableError):
        BinanceAdapter(testnet=False, market="spot").fetch_funding_history("BTCUSDT", 0, 1)


@responses.activate
def test_fetch_aggregate_trades_preserves_native_ids_and_raw_pages():
    start = 1_700_000_000_000
    responses.add(
        responses.GET,
        "https://fapi.binance.com/fapi/v1/aggTrades",
        json=[
            {"a": 41, "p": "100.1", "q": "0.2", "T": start, "m": True},
            {"a": 42, "p": "100.2", "q": "0.3", "T": start + 1, "m": False},
        ],
    )

    evidence = BinanceAdapter(testnet=False, market="future").fetch_aggregate_trades(
        "BTC/USDT", start, start + 1
    )

    assert [record.aggregate_trade_id for record in evidence.records] == [41, 42]
    assert evidence.records[0].price == Decimal("100.1")
    assert evidence.records[0].buyer_is_maker is True
    assert evidence.coverage_start_ms == start
    assert evidence.coverage_end_ms == start + 1
    assert evidence.raw_responses[0][0]["a"] == 41


@responses.activate
def test_fetch_aggregate_trades_rejects_a_native_sequence_gap():
    start = 1_700_000_000_000
    responses.add(
        responses.GET,
        "https://fapi.binance.com/fapi/v1/aggTrades",
        json=[
            {"a": 41, "p": "100.1", "q": "0.2", "T": start, "m": True},
            {"a": 43, "p": "100.2", "q": "0.3", "T": start + 1, "m": False},
        ],
    )

    with pytest.raises(ValueError, match="aggregate-trade sequence gap"):
        BinanceAdapter(testnet=False, market="future").fetch_aggregate_trades(
            "BTCUSDT", start, start + 1
        )


@responses.activate
def test_fetch_order_book_snapshot_preserves_exchange_sequence_and_timestamp():
    responses.add(
        responses.GET,
        "https://fapi.binance.com/fapi/v1/depth",
        json={
            "lastUpdateId": 91,
            "E": 1_700_000_000_010,
            "T": 1_700_000_000_009,
            "bids": [["99.9", "2"], ["99.8", "3"]],
            "asks": [["100.1", "1"], ["100.2", "4"]],
        },
    )

    evidence = BinanceAdapter(testnet=False, market="future").fetch_order_book_snapshot(
        "BTCUSDT", limit=100
    )

    assert evidence.snapshot.source_sequence == 91
    assert evidence.snapshot.timestamp_ms == 1_700_000_000_009
    assert evidence.snapshot.bids[0].price == Decimal("99.9")
    assert evidence.observed_at_ms == 1_700_000_000_010
    assert evidence.raw_response["lastUpdateId"] == 91


def test_trade_and_depth_evidence_are_futures_only():
    adapter = BinanceAdapter(testnet=False, market="spot")

    with pytest.raises(ValueError, match="Futures only"):
        adapter.fetch_aggregate_trades("BTCUSDT", 0, 1)
    with pytest.raises(ValueError, match="Futures only"):
        adapter.fetch_order_book_snapshot("BTCUSDT")


@responses.activate
def test_aggregate_trade_rejects_non_boolean_maker_flag():
    responses.add(
        responses.GET,
        "https://fapi.binance.com/fapi/v1/aggTrades",
        json=[{"a": 41, "p": "100.1", "q": "0.2", "T": 1, "m": "false"}],
    )

    with pytest.raises(ValueError, match="aggregate-trade record"):
        BinanceAdapter(testnet=False).fetch_aggregate_trades("BTCUSDT", 0, 1)


def _exchange_info():
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "marginAsset": "USDT",
                "liquidationFee": "0.005",
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.10",
                        "maxPrice": "1000000",
                        "tickSize": "0.10",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "1000",
                        "stepSize": "0.001",
                    },
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    {
                        "filterType": "PERCENT_PRICE",
                        "multiplierDown": "0.85",
                        "multiplierUp": "1.15",
                    },
                ],
            }
        ]
    }


def _leverage_brackets():
    return [
        {
            "symbol": "BTCUSDT",
            "brackets": [
                {
                    "bracket": 1,
                    "initialLeverage": 125,
                    "notionalCap": "50000",
                    "notionalFloor": "0",
                    "maintMarginRatio": "0.004",
                    "cum": "0",
                },
                {
                    "bracket": 2,
                    "initialLeverage": 100,
                    "notionalCap": "250000",
                    "notionalFloor": "50000",
                    "maintMarginRatio": "0.005",
                    "cum": "50",
                },
            ],
        }
    ]


@responses.activate
def test_read_only_evidence_client_normalizes_instrument_rules_and_margin_tiers():
    responses.add(
        responses.GET,
        "https://fapi.binance.com/fapi/v1/exchangeInfo",
        json=_exchange_info(),
    )
    responses.add(
        responses.GET,
        "https://fapi.binance.com/fapi/v1/leverageBracket",
        json=_leverage_brackets(),
    )
    client = BinanceFuturesEvidenceClient(
        api_key="read-only-key",
        api_secret="read-only-secret",
        clock_ms=lambda: 1_700_000_000_000,
    )

    spec = client.fetch_instrument_spec("BTC/USDT")

    assert spec.exchange == "binance"
    assert spec.market == "future"
    assert spec.canonical_symbol == "BTCUSDT"
    assert spec.effective_from_ms == 1_700_000_000_000
    assert spec.effective_to_ms is None
    assert spec.evidence_status == "current_snapshot"
    assert spec.tick_size == Decimal("0.10")
    assert spec.step_size == Decimal("0.001")
    assert spec.minimum_notional == Decimal("5")
    assert spec.collateral_currency == "USDT"
    assert spec.liquidation_fee_bps == Decimal("50.000")
    assert [tier.maintenance_margin_rate for tier in spec.margin_tiers] == [
        Decimal("0.004"),
        Decimal("0.005"),
    ]
    assert [tier.maximum_leverage for tier in spec.margin_tiers] == [
        Decimal("125"),
        Decimal("100"),
    ]
    assert spec.margin_tiers[1].maintenance_amount == Decimal("50")
    assert spec.price_band_low_multiplier == Decimal("0.85")
    assert spec.price_band_high_multiplier == Decimal("1.15")
    assert spec.raw_response["exchange_info"]["symbol"] == "BTCUSDT"
    assert spec.raw_response["leverage_bracket"]["symbol"] == "BTCUSDT"
    assert len(responses.calls) == 2
    signed = responses.calls[1].request
    assert signed.method == "GET"
    assert "signature=" in signed.url
    assert signed.headers["X-MBX-APIKEY"] == "read-only-key"


@responses.activate
def test_read_only_evidence_client_normalizes_current_account_commission():
    responses.add(
        responses.GET,
        "https://fapi.binance.com/fapi/v1/commissionRate",
        json={
            "symbol": "BTCUSDT",
            "makerCommissionRate": "0.0002",
            "takerCommissionRate": "0.0005",
        },
    )
    client = BinanceFuturesEvidenceClient(
        api_key="read-only-key",
        api_secret="read-only-secret",
        clock_ms=lambda: 1_700_000_000_000,
    )

    fee = client.fetch_fee_schedule("BTCUSDT")

    assert fee.maker_bps == 2.0
    assert fee.taker_bps == 5.0
    assert fee.currency == "USDT"
    assert fee.evidence_status == "current_snapshot"
    assert fee.effective_from_ms == 1_700_000_000_000
    assert fee.effective_to_ms == 1_700_000_000_000
    assert fee.discount_treatment == "account_rate_observed"
    assert "read-only-key" not in repr(fee.raw_response)
    assert "read-only-secret" not in repr(fee.raw_response)


def test_read_only_evidence_client_rejects_invalid_or_missing_credentials():
    with pytest.raises(ValueError, match="credentials"):
        BinanceFuturesEvidenceClient(api_key="", api_secret="secret")
    with pytest.raises(ValueError, match="credentials"):
        BinanceFuturesEvidenceClient(api_key="key", api_secret="")
