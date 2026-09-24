"""Deterministic L2 depth and queue execution primitives."""

from decimal import Decimal

import pytest

from koval.engine.market_depth_execution import (
    BookDelta,
    BookLevel,
    BookLevelChange,
    DepthOrderIntent,
    LimitQueueModel,
    OrderBookSnapshot,
    OrderBookState,
    QueueEstimatePolicy,
    execute_against_depth,
)

D = Decimal
T0 = 1_700_000_000_000


def book() -> OrderBookSnapshot:
    return OrderBookSnapshot(
        timestamp_ms=T0,
        source="binance_usdm_depth",
        source_sequence=11,
        bids=(BookLevel(D("99"), D("4")), BookLevel(D("98"), D("10"))),
        asks=(BookLevel(D("101"), D("2")), BookLevel(D("102"), D("3"))),
    )


def test_market_buy_walks_asks_and_reports_explainable_vwap():
    result = execute_against_depth(
        book(),
        DepthOrderIntent(
            order_id="buy-1",
            side="buy",
            order_type="market",
            quantity=D("4"),
            eligible_timestamp_ms=T0,
        ),
    )

    assert result.status == "filled"
    assert [(fill.price, fill.quantity) for fill in result.fills] == [
        (D("101"), D("2")),
        (D("102"), D("2")),
    ]
    assert result.vwap == D("101.5")
    assert result.remaining_quantity == 0
    assert result.book_sequence == 11


def test_market_order_returns_partial_instead_of_inventing_liquidity():
    result = execute_against_depth(
        book(),
        DepthOrderIntent(
            order_id="buy-too-large",
            side="buy",
            order_type="market",
            quantity=D("8"),
            eligible_timestamp_ms=T0,
        ),
    )

    assert result.status == "insufficient_liquidity"
    assert result.filled_quantity == 5
    assert result.remaining_quantity == 3


def test_fok_limit_is_all_or_nothing_and_ioc_cancels_only_the_remainder():
    fok = execute_against_depth(
        book(),
        DepthOrderIntent(
            order_id="fok",
            side="buy",
            order_type="limit",
            quantity=D("6"),
            limit_price=D("102"),
            time_in_force="FOK",
            eligible_timestamp_ms=T0,
        ),
    )
    ioc = execute_against_depth(
        book(),
        DepthOrderIntent(
            order_id="ioc",
            side="buy",
            order_type="limit",
            quantity=D("6"),
            limit_price=D("102"),
            time_in_force="IOC",
            eligible_timestamp_ms=T0,
        ),
    )

    assert fok.status == "fok_unfilled"
    assert fok.fills == ()
    assert ioc.status == "ioc_remainder_cancelled"
    assert ioc.filled_quantity == 5
    assert ioc.remaining_quantity == 1


def test_post_only_crossing_order_is_rejected_without_a_hidden_fill():
    result = execute_against_depth(
        book(),
        DepthOrderIntent(
            order_id="post-only",
            side="buy",
            order_type="limit",
            quantity=D("1"),
            limit_price=D("101"),
            post_only=True,
            eligible_timestamp_ms=T0,
        ),
    )

    assert result.status == "post_only_would_cross"
    assert result.fills == ()


def test_latency_blocks_a_snapshot_before_order_eligibility():
    result = execute_against_depth(
        book(),
        DepthOrderIntent(
            order_id="late",
            side="sell",
            order_type="market",
            quantity=D("1"),
            eligible_timestamp_ms=T0 + 1,
        ),
    )

    assert result.status == "not_yet_eligible"


def test_reduce_only_never_increases_or_reverses_exposure():
    with pytest.raises(ValueError, match="reduce-only quantity exceeds"):
        execute_against_depth(
            book(),
            DepthOrderIntent(
                order_id="reduce",
                side="sell",
                order_type="market",
                quantity=D("3"),
                reduce_only=True,
                eligible_timestamp_ms=T0,
            ),
            position_quantity=D("2"),
            position_side="buy",
        )


def test_limit_queue_consumes_ahead_volume_before_filling_and_versions_cancellation_credit():
    queue = LimitQueueModel(
        order_id="resting-buy",
        side="buy",
        limit_price=D("99"),
        quantity=D("3"),
        queue_ahead=D("5"),
        policy=QueueEstimatePolicy(
            version="binance_fifo_expected_v1",
            scenario="expected",
            cancellation_credit=D("0.5"),
        ),
    )

    assert queue.apply_trade(price=D("99"), quantity=D("4")) == 0
    assert queue.apply_cancellation(price=D("99"), quantity=D("2")) == D("1")
    assert queue.apply_trade(price=D("99"), quantity=D("2")) == D("2")
    assert queue.snapshot() == {
        "order_id": "resting-buy",
        "policy_version": "binance_fifo_expected_v1",
        "scenario": "expected",
        "queue_ahead": "0",
        "filled_quantity": "2",
        "remaining_quantity": "1",
    }


def test_binance_book_state_applies_bridging_and_contiguous_deltas():
    state = OrderBookState(book())

    state.apply_delta(
        BookDelta(
            timestamp_ms=T0 + 1,
            source="binance_usdm_depth_stream",
            first_sequence=10,
            final_sequence=12,
            previous_final_sequence=9,
            bids=(BookLevelChange(D("99"), D("0")),),
            asks=(BookLevelChange(D("101"), D("5")),),
        )
    )
    updated = state.apply_delta(
        BookDelta(
            timestamp_ms=T0 + 2,
            source="binance_usdm_depth_stream",
            first_sequence=13,
            final_sequence=13,
            previous_final_sequence=12,
            bids=(BookLevelChange(D("97"), D("7")),),
            asks=(),
        )
    )

    assert updated.source_sequence == 13
    assert [(level.price, level.quantity) for level in updated.bids] == [
        (D("98"), D("10")),
        (D("97"), D("7")),
    ]
    assert updated.asks[0] == BookLevel(D("101"), D("5"))


def test_book_state_rejects_gap_duplicate_and_out_of_order_delta():
    state = OrderBookState(book())
    with pytest.raises(ValueError, match="does not bridge"):
        state.apply_delta(
            BookDelta(
                timestamp_ms=T0 + 1,
                source="binance_usdm_depth_stream",
                first_sequence=13,
                final_sequence=13,
                previous_final_sequence=12,
                bids=(),
                asks=(BookLevelChange(D("101"), D("2")),),
            )
        )

    state.apply_delta(
        BookDelta(
            timestamp_ms=T0 + 1,
            source="binance_usdm_depth_stream",
            first_sequence=12,
            final_sequence=12,
            previous_final_sequence=11,
            bids=(),
            asks=(BookLevelChange(D("101"), D("2")),),
        )
    )
    with pytest.raises(ValueError, match="duplicate or stale"):
        state.apply_delta(
            BookDelta(
                timestamp_ms=T0 + 2,
                source="binance_usdm_depth_stream",
                first_sequence=12,
                final_sequence=12,
                previous_final_sequence=11,
                bids=(),
                asks=(BookLevelChange(D("101"), D("2")),),
            )
        )
    with pytest.raises(ValueError, match="timestamp regression"):
        state.apply_delta(
            BookDelta(
                timestamp_ms=T0,
                source="binance_usdm_depth_stream",
                first_sequence=13,
                final_sequence=13,
                previous_final_sequence=12,
                bids=(),
                asks=(BookLevelChange(D("101"), D("2")),),
            )
        )
