from decimal import Decimal

import pytest

from koval.engine.broker import BrokerOrderIntent
from koval.engine.funding import FundingRecord, build_funding_series
from koval.engine.paper_broker import PaperBroker, PaperOrderRejected
from koval.engine.paper_profile import (
    PAPER_FIXED_VERSION,
    PAPER_REALISTIC_VERSION,
    resolve_paper_profile,
)

T0 = 1_704_067_200_000
H = 3_600_000
FIXED = resolve_paper_profile(
    {
        "version": PAPER_FIXED_VERSION,
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 10.0,
    }
)
FEES_ONLY_5X = resolve_paper_profile(
    {
        "version": PAPER_FIXED_VERSION,
        "commission_bps": 4.0,
        "spread_bps": 0.0,
        "slippage_bps": 0.0,
        "leverage": 5.0,
    }
)
REALISTIC = resolve_paper_profile(
    {
        "version": PAPER_REALISTIC_VERSION,
        "commission_bps": 4.0,
        "spread_bps": 20.0,
        "slippage_bps": 10.0,
    }
)


def _long(broker, *, size=2.0, entry=100.0, stop=90.0, target=120.0):
    broker.submit_bracket(
        side="buy",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=size,
        order_type="market",
    )


def test_market_entry_fills_at_next_open_with_adverse_cost_and_commission():
    b = PaperBroker(10_000.0, profile=FIXED)
    _long(b)
    assert b.fill_market_if_pending(ts_ms=T0, price=100.0) is None
    fills = b.process_bar(ts_ms=T0 + H, open=100.0, high=100.5, low=99.5, close=100.0)
    (entry,) = fills
    assert entry.kind == "entry"
    assert entry.price == pytest.approx(100.20)
    assert entry.reference_price == 100.0
    assert entry.commission == pytest.approx(0.08016)
    assert entry.realized_pnl == pytest.approx(-0.08016)
    assert b.balance == pytest.approx(10_000 - 0.08016)
    assert b.position.entry_commission == pytest.approx(0.08016)
    assert b.margin_used == pytest.approx(200.40)


def test_gap_stop_fixture_reconciles_to_9969_111976():
    b = PaperBroker(10_000.0, profile=FIXED)
    _long(b)
    b.process_bar(ts_ms=T0 + H, open=100.0, high=100.5, low=99.5, close=100.0)
    (exit_fill,) = b.process_bar(ts_ms=T0 + 2 * H, open=85.0, high=86.0, low=84.0, close=85.0)
    assert exit_fill.kind == "stop_loss"
    assert exit_fill.reference_price == 85.0
    assert exit_fill.price == pytest.approx(84.83)
    assert exit_fill.commission == pytest.approx(0.067864)
    assert exit_fill.spread_cost == pytest.approx(0.17)
    assert exit_fill.slippage_cost == pytest.approx(0.17)
    assert exit_fill.realized_pnl == pytest.approx(-30.74 - 0.067864)
    assert b.balance == pytest.approx(9969.111976, abs=1e-9)
    assert b.equity == pytest.approx(9969.111976, abs=1e-9)


def test_exits_are_not_evaluated_on_the_entry_fill_bar():
    b = PaperBroker(10_000.0, profile=FIXED)
    _long(b)
    fills = b.process_bar(ts_ms=T0 + H, open=100.0, high=125.0, low=80.0, close=100.0)
    assert [f.kind for f in fills] == ["entry"]
    assert b.position is not None


def test_take_profit_is_market_on_touch_with_full_adverse_cost():
    b = PaperBroker(10_000.0, profile=FIXED)
    _long(b)
    b.process_bar(ts_ms=T0 + H, open=100.0, high=100.5, low=99.5, close=100.0)
    (tp,) = b.process_bar(ts_ms=T0 + 2 * H, open=110.0, high=121.0, low=109.0, close=120.0)
    assert tp.kind == "take_profit" and tp.reference_price == 120.0
    assert tp.price == pytest.approx(119.76)
    assert b.balance == pytest.approx(10038.944032, abs=1e-9)


def test_favorable_open_is_the_take_profit_reference():
    b = PaperBroker(10_000.0, profile=FEES_ONLY_5X)
    _long(b, size=200.0, target=108.0)
    b.process_bar(ts_ms=T0 + H, open=100.0, high=100.5, low=99.5, close=100.0)
    (tp,) = b.process_bar(ts_ms=T0 + 2 * H, open=108.0, high=108.5, low=107.5, close=108.0)
    assert tp.reference_price == 108.0 and tp.price == pytest.approx(108.0)
    assert b.balance == pytest.approx(11583.36, abs=1e-9)


def test_short_leverage_fixture_reconciles_to_8383_36():
    b = PaperBroker(10_000.0, profile=FEES_ONLY_5X)
    b.submit_bracket(
        side="sell",
        entry_price=100.0,
        stop_price=108.0,
        target_price=92.0,
        quantity=200.0,
        order_type="market",
    )
    b.process_bar(ts_ms=T0 + H, open=100.0, high=100.5, low=99.5, close=100.0)
    assert b.margin_used == pytest.approx(4000.0)
    (stop,) = b.process_bar(ts_ms=T0 + 2 * H, open=108.0, high=108.5, low=107.5, close=108.0)
    assert stop.kind == "stop_loss" and stop.price == pytest.approx(108.0)
    assert b.balance == pytest.approx(8383.36, abs=1e-9)


def test_insufficient_margin_rejects_without_halting_the_broker():
    b = PaperBroker(10_000.0, profile=FEES_ONLY_5X)
    with pytest.raises(PaperOrderRejected, match="insufficient_margin"):
        _long(b, size=600.0)  # 60000 / 5 = 12000 > 10000
    assert b.pending is False
    intent = BrokerOrderIntent(
        session_id="s",
        intent_id="i",
        client_order_id="kv-1",
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        quantity="600",
        target="paper",
        price="100",
        stop_price="90",
        target_price="108",
    )
    ack = b.submit_entry(intent)
    assert ack.status == "rejected" and ack.metadata["reason"] == "insufficient_margin"


def test_equity_marks_open_position_with_fees_already_paid():
    b = PaperBroker(10_000.0, profile=FEES_ONLY_5X)
    _long(b, size=200.0, target=108.0)
    b.process_bar(ts_ms=T0 + H, open=100.0, high=100.5, low=99.5, close=100.0)
    b.process_bar(ts_ms=T0 + 2 * H, open=102.0, high=102.5, low=101.5, close=102.0)
    assert b.equity == pytest.approx(10_000 - 8.0 + 400.0)


def test_flatten_applies_costs_under_the_fixed_profile():
    b = PaperBroker(10_000.0, profile=FIXED)
    _long(b)
    b.process_bar(ts_ms=T0 + H, open=100.0, high=100.5, low=99.5, close=100.0)
    fill = b.flatten(ts_ms=T0 + 2 * H, price=100.0)
    assert fill.kind == "manual_close"
    assert fill.reference_price == 100.0
    assert fill.price == pytest.approx(99.80)
    assert fill.commission == pytest.approx(2.0 * 99.80 * 4 / 10_000)


def test_legacy_profile_behavior_is_unchanged():
    b = PaperBroker(10_000.0)
    _long(b)
    fill = b.fill_market_if_pending(ts_ms=T0, price=100.0)
    assert fill is not None and fill.price == 100.0 and fill.commission == 0.0


def test_fill_time_affordability_rejects_when_adverse_adjustment_exceeds_margin():
    """An order can clear ``submit_bracket``'s nominal-price check and still be
    unaffordable at the adverse-adjusted fill price. The re-check at fill time
    must drop it rather than post a position into negative free margin."""
    b = PaperBroker(10_000.0, profile=FIXED)  # leverage 1, buy fills 0.2% above open
    _long(b, size=99.9, entry=100.0, stop=90.0, target=120.0)  # 9993.996 <= 10000, passes
    assert b.pending is True

    fills = b.process_bar(ts_ms=T0 + H, open=100.0, high=100.5, low=99.5, close=100.0)

    assert fills == []
    assert b.position is None
    assert b.pending is False
    assert b.equity - b.margin_used == pytest.approx(10_000.0)
    assert b.consume_entry_rejection() is not None
    assert b.consume_entry_rejection() is None


@pytest.mark.parametrize(
    ("side", "entry", "stop", "target", "bar", "expected_reference", "expected_fill"),
    [
        ("buy", 100.0, 90.0, 120.0, (95.0, 99.0, 94.0, 97.0), 95.0, 95.19),
        ("sell", 100.0, 110.0, 80.0, (105.0, 106.0, 101.0, 103.0), 105.0, 104.79),
    ],
)
def test_limit_entry_uses_favorable_open_even_without_literal_limit_cross(
    side, entry, stop, target, bar, expected_reference, expected_fill
):
    broker = PaperBroker(10_000.0, profile=FIXED)
    broker.submit_bracket(
        side=side,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=1.0,
        order_type="limit",
    )

    fills = broker.process_bar(
        ts_ms=T0 + H,
        open=bar[0],
        high=bar[1],
        low=bar[2],
        close=bar[3],
    )

    (fill,) = fills
    assert fill.kind == "entry"
    assert fill.reference_price == expected_reference
    assert fill.price == pytest.approx(expected_fill)
    if side == "buy":
        assert fill.price <= entry
    else:
        assert fill.price >= entry


@pytest.mark.parametrize(
    ("side", "bar"),
    [
        ("buy", (100.0, 125.0, 80.0, 100.0)),
        ("sell", (100.0, 120.0, 75.0, 100.0)),
    ],
)
def test_realistic_profile_activates_protection_on_entry_bar_and_reports_ambiguity(side, bar):
    broker = PaperBroker(10_000.0, profile=REALISTIC)
    broker.submit_bracket(
        side=side,
        entry_price=100.0,
        stop_price=90.0 if side == "buy" else 110.0,
        target_price=120.0 if side == "buy" else 80.0,
        quantity=1.0,
        order_type="market",
    )

    fills = broker.process_bar(
        ts_ms=T0 + H,
        open=bar[0],
        high=bar[1],
        low=bar[2],
        close=bar[3],
    )

    assert [fill.kind for fill in fills] == ["entry", "stop_loss"]
    assert broker.position is None
    assert broker.resolved_metadata["ambiguity_policy"] == "conservative_stop_first"
    assert broker.resolved_metadata["ambiguities"] == [
        {
            "timestamp_ms": T0 + H,
            "reason_code": "both_protective_levels_touched",
            "selected": "stop_loss",
        }
    ]


@pytest.mark.parametrize(
    ("side", "bar_open", "expected_kind"),
    [
        ("buy", 80.0, "stop_loss"),
        ("buy", 125.0, "take_profit"),
        ("sell", 120.0, "stop_loss"),
        ("sell", 75.0, "take_profit"),
    ],
)
def test_realistic_market_gap_beyond_bracket_exits_at_market_not_unreachable_level(
    side, bar_open, expected_kind
):
    broker = PaperBroker(10_000.0, profile=REALISTIC)
    broker.submit_bracket(
        side=side,
        entry_price=100.0,
        stop_price=90.0 if side == "buy" else 110.0,
        target_price=120.0 if side == "buy" else 80.0,
        quantity=1.0,
        order_type="market",
    )

    fills = broker.process_bar(
        ts_ms=T0 + H,
        open=bar_open,
        high=bar_open + 1.0,
        low=bar_open - 1.0,
        close=bar_open,
    )

    assert [fill.kind for fill in fills] == ["entry", expected_kind]
    assert fills[1].reference_price == bar_open
    assert broker.position is None
    assert broker.balance < 10_000.0


def test_fixed_v1_keeps_delayed_protection_after_realistic_v2_is_added():
    broker = PaperBroker(10_000.0, profile=FIXED)
    _long(broker)

    fills = broker.process_bar(
        ts_ms=T0 + H,
        open=100.0,
        high=125.0,
        low=80.0,
        close=100.0,
    )

    assert [fill.kind for fill in fills] == ["entry"]
    assert broker.position is not None


def test_realistic_profile_resizes_actual_quantity_to_preserve_requested_stop_risk():
    broker = PaperBroker(10_000.0, profile=REALISTIC)
    broker.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=90.0,
        target_price=120.0,
        quantity=2.0,
        order_type="market",
        risk_budget=20.0,
        client_order_id="entry-risk",
    )

    (entry,) = broker.process_bar(
        ts_ms=T0 + H,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
    )
    (stop,) = broker.process_bar(
        ts_ms=T0 + 2 * H,
        open=90.0,
        high=91.0,
        low=89.0,
        close=90.0,
    )

    assert entry.quantity < 2.0
    assert broker.balance == pytest.approx(9_980.0)
    assert [item.kind for item in broker.ledger.entries] == [
        "commission",
        "trade_pnl",
        "commission",
    ]
    assert broker.reconcile_ledger().balanced


def test_funding_settles_before_same_timestamp_entry_and_reconciles_separately():
    funding = build_funding_series(
        [
            FundingRecord(
                symbol="BTCUSDT",
                rate=Decimal("0.001"),
                settlement_timestamp_ms=T0 + H,
                settlement_mark_price=Decimal("100"),
                interval_ms=H,
                source="fixture",
            ),
            FundingRecord(
                symbol="BTCUSDT",
                rate=Decimal("0.001"),
                settlement_timestamp_ms=T0 + 2 * H,
                settlement_mark_price=Decimal("105"),
                interval_ms=H,
                source="fixture",
            ),
        ],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=T0 + H,
        requested_end_ms=T0 + 2 * H,
    )
    broker = PaperBroker(10_000.0, profile=REALISTIC, funding=funding)
    _long(broker, size=2.0)

    (entry,) = broker.process_bar(
        ts_ms=T0 + H,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
    )
    broker.process_bar(
        ts_ms=T0 + 2 * H,
        open=105.0,
        high=106.0,
        low=104.0,
        close=105.0,
    )

    assert entry.kind == "entry"
    assert broker.ledger.funding == pytest.approx(-0.21)
    assert broker.resolved_metadata["funding_status"] == "historical"
    assert broker.reconcile_ledger().balanced


def test_paper_broker_rejects_order_symbol_that_disagrees_with_funding_evidence():
    funding = build_funding_series(
        [
            FundingRecord(
                symbol="BTCUSDT",
                rate=Decimal("0.001"),
                settlement_timestamp_ms=T0 + H,
                settlement_mark_price=Decimal("100"),
                interval_ms=H,
                source="fixture",
            )
        ],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=T0 + H,
        requested_end_ms=T0 + H,
    )
    broker = PaperBroker(10_000.0, profile=REALISTIC, funding=funding)

    with pytest.raises(ValueError, match="evidence symbol mismatch"):
        broker.submit_bracket(
            side="buy",
            entry_price=100,
            stop_price=90,
            target_price=120,
            quantity=1,
            order_type="market",
            symbol="ETHUSDT",
        )
