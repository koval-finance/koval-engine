"""Coverage claims must survive construction and venue normalization."""

from dataclasses import replace
from decimal import Decimal

import pytest
import responses

from koval.engine.funding import FundingRecord, build_funding_series
from koval.exchanges.whitebit import WhiteBITAdapter

H8 = 28_800_000


def record(ts=H8, symbol="BTCUSDT"):
    return FundingRecord(symbol, Decimal("0.001"), ts, Decimal("100"), H8, "archive")


def test_empty_window_needs_settlement_phase_evidence_not_only_an_interval():
    with pytest.raises(ValueError, match="anchor"):
        build_funding_series(
            [],
            exchange="binance",
            market="future",
            symbol="BTCUSDT",
            requested_start_ms=H8,
            requested_end_ms=H8 + 1,
            interval_ms=H8,
        )


@pytest.mark.parametrize("records", [[record(symbol="ETHUSDT")], [record(H8 - 1)]])
def test_funding_rejects_wrong_instrument_or_outside_requested_coverage(records):
    with pytest.raises(ValueError, match="funding"):
        build_funding_series(
            records,
            exchange="binance",
            market="future",
            symbol="BTCUSDT",
            requested_start_ms=H8,
            requested_end_ms=H8 + 1,
        )


def test_complete_funding_cannot_be_forged_by_replacing_records():
    series = build_funding_series(
        [record(), record(2 * H8)],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=H8,
        requested_end_ms=2 * H8,
    )
    with pytest.raises(ValueError, match="coverage"):
        replace(series, records=())


@responses.activate
def test_whitebit_funding_repeated_full_page_stops_at_second_response():
    rows = [
        {
            "fundingTime": str((i + 1) * 28800),
            "fundingRate": "0.001",
            "market": "BTC_PERP",
            "settlementPrice": "100",
            "rateCalculatedTime": str(i * 28800),
        }
        for i in reversed(range(100))
    ]
    url = "https://whitebit.com/api/v4/public/funding-history/BTC_PERP"
    responses.get(url, json=rows)
    responses.get(url, json=rows)
    responses.get(url, json=[])
    with pytest.raises(RuntimeError, match="funding pagination"):
        WhiteBITAdapter(market="future").fetch_funding_history("BTCUSDT", H8, 200 * H8)
    assert len(responses.calls) == 2


def test_bound_fee_evidence_cannot_cross_venue_or_market():
    from koval.engine.fee_evidence import FeeScheduleEvidence
    from koval.engine.paper_broker import PaperBroker
    from koval.engine.paper_profile import resolve_paper_profile
    from tests.engine.test_runtime_contract import EXECUTION

    assert "exchange" in FeeScheduleEvidence.__dataclass_fields__
    schedule = FeeScheduleEvidence(
        "fees",
        1,
        4,
        "USDT",
        "historical",
        "archive",
        0,
        60_000,
        exchange="binance",
        market="future",
        canonical_symbol="BTCUSDT",
    )
    with pytest.raises(ValueError, match="exchange"):
        PaperBroker(
            10_000,
            exchange="whitebit",
            profile=resolve_paper_profile(EXECUTION),
            fee_schedule=schedule,
        )
    with pytest.raises(ValueError, match="market"):
        PaperBroker(
            10_000, market="spot", profile=resolve_paper_profile(EXECUTION), fee_schedule=schedule
        )


def test_mark_series_cannot_claim_complete_with_missing_price():
    from koval.engine.instrument_risk import MarkPriceRecord, MarkPriceSeries

    with pytest.raises(ValueError, match="coverage"):
        MarkPriceSeries(
            "binance",
            "BTCUSDT",
            60_000,
            0,
            120_000,
            (MarkPriceRecord(0, Decimal("100"), "archive"),),
            True,
        )


@responses.activate
def test_whitebit_fees_are_current_snapshot_with_actual_market_identity():
    assert hasattr(WhiteBITAdapter, "fetch_fee_schedule")
    responses.get(
        "https://whitebit.com/api/v4/public/markets",
        json=[
            {
                "name": "BTC_PERP",
                "type": "futures",
                "makerFee": "0.1",
                "takerFee": "0.04",
                "money": "USDT",
                "stock": "BTC",
                "stockPrec": 3,
                "moneyPrec": 1,
            }
        ],
    )
    evidence = WhiteBITAdapter(market="future").fetch_fee_schedule("BTCUSDT")
    assert evidence.evidence_status == "current_snapshot"
    assert evidence.exchange == "whitebit"
    assert evidence.market == "future"
    assert evidence.maker_bps == 10
    assert evidence.taker_bps == 4
    assert evidence.effective_from_ms == evidence.effective_to_ms
    assert evidence.raw_response


@responses.activate
def test_single_whitebit_rate_calculation_time_is_not_a_settlement_schedule():
    responses.get(
        "https://whitebit.com/api/v4/public/funding-history/BTC_PERP",
        json=[
            {
                "fundingTime": "28800",
                "rateCalculatedTime": "28799",
                "fundingRate": "0.0001",
                "settlementPrice": "100",
                "market": "BTC_PERP",
            }
        ],
    )
    with pytest.raises(ValueError, match="interval.*unavailable"):
        WhiteBITAdapter(market="future").fetch_funding_history("BTCUSDT", H8, H8)


@responses.activate
def test_binance_mark_history_uses_mark_open_not_future_close():
    from koval.exchanges.binance import BinanceAdapter

    assert hasattr(BinanceAdapter, "fetch_mark_prices")
    responses.get(
        "https://fapi.binance.com/fapi/v1/markPriceKlines",
        json=[[0, "100", "110", "90", "105", "0"], [60_000, "105", "120", "100", "115", "0"]],
    )
    series = BinanceAdapter(testnet=False).fetch_mark_prices("BTCUSDT", "1m", 0, 60_000)
    assert series.at(0).price == Decimal("100")
    assert series.at(60_000).price == Decimal("105")
    assert series.raw_responses
