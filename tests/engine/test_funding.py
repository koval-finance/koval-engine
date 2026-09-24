from decimal import Decimal

import pytest

from koval.engine.funding import FundingRecord, build_funding_series, funding_cashflow

H8 = 8 * 60 * 60 * 1000


def _record(timestamp, rate="0.0001", mark="100"):
    return FundingRecord(
        symbol="BTCUSDT",
        rate=Decimal(rate),
        settlement_timestamp_ms=timestamp,
        settlement_mark_price=Decimal(mark),
        interval_ms=H8,
        source="test",
    )


def test_funding_series_requires_every_settlement_in_requested_coverage():
    series = build_funding_series(
        [_record(0), _record(H8), _record(2 * H8)],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=0,
        requested_end_ms=2 * H8,
        raw_responses=({"page": 1},),
    )
    assert series.coverage_complete
    assert series.raw_responses == ({"page": 1},)

    with pytest.raises(ValueError, match="incomplete funding coverage"):
        build_funding_series(
            [_record(0), _record(2 * H8)],
            exchange="binance",
            market="future",
            symbol="BTCUSDT",
            requested_start_ms=0,
            requested_end_ms=2 * H8,
        )


@pytest.mark.parametrize(
    ("side", "rate", "expected"),
    [
        ("buy", "0.0001", "-0.02"),
        ("sell", "0.0001", "0.02"),
        ("buy", "-0.0001", "0.02"),
        ("sell", "-0.0001", "-0.02"),
    ],
)
def test_funding_cashflow_has_exchange_sign_convention(side, rate, expected):
    assert funding_cashflow(
        side=side,
        quantity=Decimal("2"),
        rate=Decimal(rate),
        mark_price=Decimal("100"),
    ) == Decimal(expected)


def test_funding_schedule_must_be_representable_on_execution_grid():
    series = build_funding_series(
        [],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=1,
        requested_end_ms=H8 // 2,
        interval_ms=H8,
        settlement_anchor_ms=0,
        schedule_source="binance_usdm_funding_rate_history",
    )

    series.validate_execution_grid(H8 // 2)
    with pytest.raises(ValueError, match="finer execution timeframe"):
        series.validate_execution_grid(3 * H8)


def test_funding_schedule_anchor_must_align_with_execution_grid():
    hour = 60 * 60 * 1000
    series = build_funding_series(
        [],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=hour + 1,
        requested_end_ms=2 * hour,
        interval_ms=H8,
        settlement_anchor_ms=hour,
        schedule_source="binance_usdm_funding_rate_history",
    )

    with pytest.raises(ValueError, match="anchor"):
        series.validate_execution_grid(4 * hour)


def test_requested_bounds_do_not_need_to_equal_settlement_timestamps():
    series = build_funding_series(
        (_record(H8),),
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=1,
        requested_end_ms=H8 + 1,
    )
    assert [record.settlement_timestamp_ms for record in series.records] == [H8]


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": ""},
        {"rate": Decimal("NaN")},
        {"settlement_timestamp_ms": True},
        {"settlement_timestamp_ms": -1},
        {"settlement_mark_price": Decimal("0")},
        {"interval_ms": 0},
        {"interval_ms": True},
        {"source": ""},
        {"rate_calculated_timestamp_ms": H8 + 1},
    ],
)
def test_funding_record_rejects_invalid_evidence(overrides):
    values = {
        "symbol": "BTCUSDT",
        "rate": Decimal("0.0001"),
        "settlement_timestamp_ms": H8,
        "settlement_mark_price": Decimal("100"),
        "interval_ms": H8,
        "source": "test",
        "rate_calculated_timestamp_ms": 0,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match="funding"):
        FundingRecord(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": Decimal("0")},
        {"quantity": Decimal("NaN")},
        {"rate": Decimal("NaN")},
        {"mark_price": Decimal("0")},
    ],
)
def test_funding_cashflow_rejects_invalid_numeric_inputs(overrides):
    values = {
        "side": "buy",
        "quantity": Decimal("1"),
        "rate": Decimal("0.0001"),
        "mark_price": Decimal("100"),
    }
    values.update(overrides)

    with pytest.raises(ValueError, match="funding"):
        funding_cashflow(**values)


def test_settlements_need_not_sit_on_an_epoch_multiple_of_the_interval():
    """Coverage is judged against the phase the venue actually settles on, not
    against an assumed epoch-aligned grid."""
    offset = 3_600_000

    series = build_funding_series(
        [_record(offset), _record(offset + H8)],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=offset - 1,
        requested_end_ms=offset + H8,
    )

    assert series.coverage_complete
    assert [record.settlement_timestamp_ms for record in series.records] == [offset, offset + H8]


def test_a_window_shorter_than_one_interval_may_hold_no_settlement():
    """A six-hour backtest between two eight-hour settlements observed zero
    funding. That is complete coverage, not missing evidence."""
    series = build_funding_series(
        [],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=H8 + 1,
        requested_end_ms=H8 + 6 * 60 * 60 * 1000,
        interval_ms=H8,
        settlement_anchor_ms=H8,
        schedule_source="archived_venue_schedule",
    )

    assert series.coverage_complete
    assert series.records == ()


def test_a_window_spanning_a_full_interval_still_requires_evidence():
    with pytest.raises(ValueError, match="incomplete funding coverage"):
        build_funding_series(
            [],
            exchange="binance",
            market="future",
            symbol="BTCUSDT",
            requested_start_ms=0,
            requested_end_ms=H8,
            interval_ms=H8,
        )


def test_a_settlement_missing_from_the_start_of_the_window_is_rejected():
    with pytest.raises(ValueError, match="incomplete funding coverage"):
        build_funding_series(
            [_record(3 * H8)],
            exchange="binance",
            market="future",
            symbol="BTCUSDT",
            requested_start_ms=0,
            requested_end_ms=3 * H8,
        )
