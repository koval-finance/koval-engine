"""A live paper order may execute only against a later observed quote."""

from decimal import Decimal

import pytest

from koval.engine.execution_evidence import ExecutionEvidenceUpdate
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.engine.funding import FundingRecord, build_funding_series
from koval.engine.instrument_risk import MarkPriceRecord
from koval.engine.paper_broker import PaperBroker
from koval.engine.paper_profile import PAPER_REALISTIC_VERSION, resolve_paper_profile

PROFILE = resolve_paper_profile(
    {
        "version": PAPER_REALISTIC_VERSION,
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 5.0,
    }
)


def _pending_long(broker: PaperBroker) -> None:
    broker.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=90.0,
        target_price=120.0,
        quantity=1.0,
        order_type="market",
    )


def test_closed_candle_cannot_fill_observed_quote_order():
    broker = PaperBroker(10_000.0, profile=PROFILE, observed_quote_execution=True)
    _pending_long(broker)

    assert broker.process_bar(ts_ms=3_600_000, open=100.0, high=130.0, low=80.0, close=105.0) == []
    assert broker.position is None


def test_observed_quote_mode_rejects_leverage_without_live_mark_liquidation():
    leveraged = resolve_paper_profile(
        {
            "version": PAPER_REALISTIC_VERSION,
            "commission_bps": 4.0,
            "spread_bps": 20.0,
            "slippage_bps": 5.0,
            "leverage": 5.0,
        }
    )
    with pytest.raises(ValueError, match="1x leverage"):
        PaperBroker(10_000.0, profile=leveraged, observed_quote_execution=True)


def test_quote_after_order_fills_at_observed_ask_with_configured_slippage():
    broker = PaperBroker(10_000.0, profile=PROFILE, observed_quote_execution=True)
    _pending_long(broker)

    with pytest.raises(ValueError, match="precedes order"):
        broker.process_observed_quote(
            received_at_ms=3_610_000,
            venue_event_ms=3_609_000,
            bid=101.0,
            ask=101.1,
            order_received_at_ms=3_610_001,
        )
    assert broker.position is None

    (fill,) = broker.process_observed_quote(
        received_at_ms=3_612_000,
        venue_event_ms=3_611_500,
        bid=101.0,
        ask=101.1,
        order_received_at_ms=3_610_001,
    )
    assert fill.kind == "entry"
    assert fill.execution_model == "observed_public_book_quote_v1"
    assert fill.timestamp_ms == 3_612_000
    assert fill.reference_price == pytest.approx(101.1)
    assert fill.price == pytest.approx(101.1 * 1.0005)
    assert fill.spread_cost == 0.0
    assert fill.slippage_cost > 0.0
    assert broker.equity == pytest.approx(
        broker.balance + (101.0 - fill.price) * fill.quantity
    )  # open long is marked to executable bid
    assert broker.resolved_metadata["execution_model"] == "observed_public_book_quote_v1"
    assert broker.resolved_metadata["partial_fill_policy"]["status"] == "not_applied"


def test_quote_outside_protection_rejects_entry_without_immediate_backdated_exit():
    broker = PaperBroker(10_000.0, profile=PROFILE, observed_quote_execution=True)
    _pending_long(broker)
    assert (
        broker.process_observed_quote(
            received_at_ms=3_612_000,
            venue_event_ms=3_611_500,
            bid=125.0,
            ask=125.1,
            order_received_at_ms=3_610_001,
        )
        == []
    )
    assert broker.consume_entry_rejection() == "quote_outside_protection"
    assert broker.position is None


def test_protection_requires_a_later_quote_and_uses_executable_bid():
    broker = PaperBroker(10_000.0, profile=PROFILE, observed_quote_execution=True)
    _pending_long(broker)
    broker.process_observed_quote(
        received_at_ms=3_612_000,
        venue_event_ms=3_611_500,
        bid=101.0,
        ask=101.1,
        order_received_at_ms=3_610_001,
    )

    assert broker.process_bar(ts_ms=3_600_000, open=100.0, high=130.0, low=80.0, close=105.0) == []
    (fill,) = broker.process_observed_quote(
        received_at_ms=3_617_000,
        venue_event_ms=3_616_500,
        bid=89.5,
        ask=89.6,
        order_received_at_ms=3_610_001,
    )
    assert fill.kind == "stop_loss"
    assert fill.execution_model == "observed_public_book_quote_v1"
    assert fill.timestamp_ms == 3_617_000
    assert fill.reference_price == pytest.approx(89.5)


def test_old_and_duplicate_quotes_fail_closed():
    broker = PaperBroker(10_000.0, profile=PROFILE, observed_quote_execution=True)
    _pending_long(broker)
    with pytest.raises(ValueError, match="stale"):
        broker.process_observed_quote(
            received_at_ms=3_620_000,
            venue_event_ms=3_600_000,
            bid=101.0,
            ask=101.1,
            order_received_at_ms=3_610_001,
        )
    broker.process_observed_quote(
        received_at_ms=3_612_000,
        venue_event_ms=3_611_500,
        bid=101.0,
        ask=101.1,
        order_received_at_ms=3_610_001,
    )
    with pytest.raises(ValueError, match="duplicate"):
        broker.process_observed_quote(
            received_at_ms=3_612_000,
            venue_event_ms=3_611_500,
            bid=101.0,
            ask=101.1,
            order_received_at_ms=3_610_001,
        )


def test_late_closed_bar_does_not_charge_funding_before_quote_entry():
    settlement = 3_600_000
    funding = build_funding_series(
        [
            FundingRecord(
                symbol="BTCUSDT",
                rate=Decimal("0.001"),
                settlement_timestamp_ms=settlement,
                settlement_mark_price=Decimal("100"),
                interval_ms=3_600_000,
                source="fixture",
            )
        ],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=settlement,
        requested_end_ms=settlement,
    )
    broker = PaperBroker(
        10_000.0,
        profile=PROFILE,
        funding=funding,
        observed_quote_execution=True,
    )
    _pending_long(broker)
    broker.process_observed_quote(
        received_at_ms=settlement + 1_000,
        venue_event_ms=settlement + 500,
        bid=101.0,
        ask=101.1,
        order_received_at_ms=settlement + 100,
    )
    balance_after_entry = broker.balance
    broker.process_bar(
        ts_ms=settlement,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
    )
    assert broker.balance == balance_after_entry


def test_quote_clock_does_not_reject_next_closed_bar_evidence_update():
    broker = PaperBroker(10_000.0, profile=PROFILE, observed_quote_execution=True)
    broker.process_bar(ts_ms=0, open=100, high=101, low=99, close=100)
    _pending_long(broker)
    broker.process_observed_quote(
        received_at_ms=3_610_000,
        venue_event_ms=3_609_500,
        bid=101.0,
        ask=101.1,
        order_received_at_ms=3_609_000,
    )
    update = ExecutionEvidenceUpdate(
        update_id="next-bar-mark",
        timestamp_ms=3_600_000,
        mark_price=MarkPriceRecord(3_600_000, Decimal("100"), "fixture"),
    )
    assert broker.apply_execution_evidence_update(update) is True


def test_quote_cursor_and_position_survive_checkpoint_without_legacy_mode_confusion():
    broker = PaperBroker(10_000.0, profile=PROFILE, observed_quote_execution=True)
    broker.process_bar(ts_ms=0, open=100, high=101, low=99, close=100)
    _pending_long(broker)
    broker.process_observed_quote(
        received_at_ms=3_610_000,
        venue_event_ms=3_609_500,
        bid=101.0,
        ask=101.1,
        order_received_at_ms=3_609_000,
    )
    checkpoint = broker.checkpoint()
    restored = PaperBroker(10_000.0, profile=PROFILE, observed_quote_execution=True)
    restored.restore_checkpoint(checkpoint)
    assert restored.checkpoint() == checkpoint
    assert restored.last_observed_quote_ms == 3_610_000
    with pytest.raises(ValueError, match="mode mismatch"):
        PaperBroker(10_000.0, profile=PROFILE).restore_checkpoint(checkpoint)


def test_archived_ohlcv_volume_proxy_is_not_applied_to_book_quote():
    proxy = ExecutionProxyConfig(
        maximum_volume_participation=Decimal("0.01"),
        entry_remainder_policy="carry",
        latency=ExecutionLatency(),
    )
    broker = PaperBroker(
        10_000.0,
        profile=PROFILE,
        execution_proxy=proxy,
        observed_quote_execution=True,
    )
    _pending_long(broker)
    fills = broker.process_observed_quote(
        received_at_ms=3_612_000,
        venue_event_ms=3_611_500,
        bid=101.0,
        ask=101.1,
        order_received_at_ms=3_610_001,
    )
    assert len(fills) == 1
    assert broker.resolved_metadata["partial_fill_policy"]["status"] == "not_applied"
