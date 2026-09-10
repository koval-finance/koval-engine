from decimal import Decimal

import pytest

from koval.engine.broker import BrokerOrderIntent
from koval.engine.execution_proxy import (
    BarLiquidityBudget,
    ExecutionLatency,
    ExecutionProxyConfig,
    ImpactCalibrationEvidence,
    execution_timeline,
    resolve_slippage_bps,
)
from koval.engine.paper_broker import PaperBroker
from koval.engine.paper_profile import PAPER_REALISTIC_VERSION, resolve_paper_profile

T0 = 1_700_000_000_000
PROFILE = resolve_paper_profile(
    {
        "version": PAPER_REALISTIC_VERSION,
        "commission_bps": 4.0,
        "spread_bps": 0.0,
        "slippage_bps": 1.0,
    }
)


def _proxy(*, participation=0.1):
    return ExecutionProxyConfig(
        maximum_volume_participation=Decimal(str(participation)),
        entry_remainder_policy="carry",
        latency=ExecutionLatency(
            decision_to_submission_ms=10,
            submission_to_acknowledgement_ms=20,
            acknowledgement_to_fill_ms=30,
            cancellation_ms=40,
            replacement_ms=50,
            protection_activation_ms=60,
        ),
    )


def test_shared_bar_budget_cannot_be_double_consumed():
    budget = BarLiquidityBudget(
        timestamp_ms=T0,
        observed_volume=Decimal("10"),
        maximum_participation=Decimal("0.20"),
    )
    assert budget.allocate(order_id="a", requested=Decimal("1.5")) == Decimal("1.5")
    assert budget.allocate(order_id="b", requested=Decimal("1")) == Decimal("0.5")
    assert budget.remaining == Decimal("0.0")
    assert budget.consumed_by_order == {"a": Decimal("1.5"), "b": Decimal("0.5")}


def test_calibrated_impact_requires_strictly_lagged_evidence():
    calibration = ImpactCalibrationEvidence(
        evidence_id="walk-forward-7",
        calibrated_at_ms=T0,
        coefficient_bps=Decimal("25"),
        volatility=Decimal("0.02"),
        source="archived_training_window",
    )
    result = resolve_slippage_bps(
        fixed_slippage_bps=Decimal("3"),
        participation=Decimal("0.25"),
        decision_timestamp_ms=T0 + 1,
        calibration=calibration,
    )
    assert result.model == "calibrated_ohlcv_proxy"
    assert result.slippage_bps == Decimal("0.250")
    with pytest.raises(ValueError, match="strictly before"):
        resolve_slippage_bps(
            fixed_slippage_bps=Decimal("3"),
            participation=Decimal("0.25"),
            decision_timestamp_ms=T0,
            calibration=calibration,
        )


def test_missing_calibration_retains_and_discloses_fixed_slippage():
    result = resolve_slippage_bps(
        fixed_slippage_bps=Decimal("3"),
        participation=Decimal("0.25"),
        decision_timestamp_ms=T0,
        calibration=None,
    )
    assert result.slippage_bps == Decimal("3")
    assert result.model == "fixed_ohlcv_proxy"
    assert result.evidence_status == "unavailable"


def test_execution_latency_timeline_is_explicit():
    timeline = execution_timeline(T0, _proxy().latency)
    assert timeline.decision_timestamp_ms == T0
    assert timeline.submission_timestamp_ms == T0 + 10
    assert timeline.acknowledgement_timestamp_ms == T0 + 30
    assert timeline.fill_eligible_timestamp_ms == T0 + 60
    assert timeline.protection_active_timestamp_ms == T0 + 120


def test_protection_latency_remains_inactive_across_multiple_bars():
    proxy = ExecutionProxyConfig(
        maximum_volume_participation=Decimal("1"),
        entry_remainder_policy="carry",
        latency=ExecutionLatency(protection_activation_ms=120_000),
    )
    broker = PaperBroker(10_000, profile=PROFILE, execution_proxy=proxy)
    broker.submit_bracket(
        side="buy",
        entry_price=100,
        stop_price=90,
        target_price=120,
        quantity=1,
        order_type="market",
        decision_timestamp_ms=T0,
    )

    entry_bar = broker.process_bar(
        ts_ms=T0,
        open=100,
        high=121,
        low=89,
        close=100,
        volume=10,
    )
    still_delayed = broker.process_bar(
        ts_ms=T0 + 60_000,
        open=100,
        high=121,
        low=89,
        close=100,
        volume=10,
    )
    activated = broker.process_bar(
        ts_ms=T0 + 120_000,
        open=100,
        high=121,
        low=89,
        close=100,
        volume=10,
    )

    assert [fill.kind for fill in entry_bar] == ["entry"]
    assert still_delayed == []
    assert [fill.kind for fill in activated] == ["stop_loss"]


def test_close_without_volume_uses_disclosed_fixed_slippage_fallback():
    calibration = ImpactCalibrationEvidence(
        evidence_id="walk-forward-7",
        calibrated_at_ms=T0 - 1,
        coefficient_bps=Decimal("25"),
        volatility=Decimal("0.02"),
        source="archived_training_window",
    )
    proxy = ExecutionProxyConfig(
        maximum_volume_participation=Decimal("1"),
        entry_remainder_policy="carry",
        latency=ExecutionLatency(),
        calibration=calibration,
    )
    broker = PaperBroker(10_000, profile=PROFILE, execution_proxy=proxy)
    broker.submit_bracket(
        side="buy",
        entry_price=100,
        stop_price=90,
        target_price=120,
        quantity=1,
        order_type="market",
        decision_timestamp_ms=T0,
    )
    broker.process_bar(
        ts_ms=T0,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=10,
    )

    fill = broker.flatten(ts_ms=T0 + 1, price=100)

    assert fill is not None
    assert fill.execution_model == "fixed_ohlcv_proxy"
    assert fill.impact_evidence_id is None
    assert fill.price == pytest.approx(99.99)


def test_paper_entry_fills_in_deltas_with_stable_order_identity():
    broker = PaperBroker(10_000, profile=PROFILE, execution_proxy=_proxy())
    broker.submit_bracket(
        side="buy",
        entry_price=100,
        stop_price=90,
        target_price=120,
        quantity=2.5,
        order_type="market",
        client_order_id="entry-partial",
    )

    first = broker.process_bar(
        ts_ms=T0,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=10,
    )[0]
    second = broker.process_bar(
        ts_ms=T0 + 60_000,
        open=101,
        high=102,
        low=100,
        close=101,
        volume=10,
    )[0]
    third = broker.process_bar(
        ts_ms=T0 + 120_000,
        open=102,
        high=103,
        low=101,
        close=102,
        volume=10,
    )[0]

    assert [
        (fill.quantity, fill.cumulative_quantity, fill.status) for fill in (first, second, third)
    ] == [
        (1.0, 1.0, "partial"),
        (1.0, 2.0, "partial"),
        (0.5, 2.5, "filled"),
    ]
    assert {fill.order_id for fill in (first, second, third)} == {"entry-partial"}
    assert broker.position.quantity == pytest.approx(2.5)
    assert broker.pending is False


def test_carried_entry_remainder_uses_only_unconsumed_stop_risk_budget():
    zero_cost_profile = resolve_paper_profile(
        {
            "version": PAPER_REALISTIC_VERSION,
            "commission_bps": 0.0,
            "spread_bps": 0.0,
            "slippage_bps": 0.0,
        }
    )
    broker = PaperBroker(
        10_000,
        profile=zero_cost_profile,
        execution_proxy=_proxy(participation=1),
    )
    broker.submit_bracket(
        side="buy",
        entry_price=100,
        stop_price=90,
        target_price=150,
        quantity=10,
        order_type="market",
        risk_budget=25,
        client_order_id="risk-limited",
    )

    broker.process_bar(
        ts_ms=T0,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
    )
    broker.process_bar(
        ts_ms=T0 + 60_000,
        open=110,
        high=111,
        low=109,
        close=110,
        volume=10,
    )

    position = broker.position
    assert position is not None
    assert position.quantity == pytest.approx(1.75)
    stop_loss = (position.entry_price - position.stop_price) * position.quantity
    assert stop_loss == pytest.approx(25.0)


def test_protective_partial_fill_resizes_position_and_cancels_entry_remainder():
    broker = PaperBroker(
        10_000,
        profile=PROFILE,
        execution_proxy=_proxy(participation=0.25),
    )
    broker.submit_bracket(
        side="buy",
        entry_price=100,
        stop_price=90,
        target_price=120,
        quantity=3,
        order_type="market",
        client_order_id="entry-partial",
    )
    broker.process_bar(
        ts_ms=T0,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=4,
    )

    (stop,) = broker.process_bar(
        ts_ms=T0 + 60_000,
        open=95,
        high=96,
        low=89,
        close=91,
        volume=2,
    )

    assert stop.kind == "stop_loss"
    assert stop.status == "partial"
    assert stop.quantity == Decimal("0.5") or stop.quantity == 0.5
    assert broker.position.quantity == pytest.approx(0.5)
    assert broker.pending is False
    assert broker.resolved_metadata["partial_fill_policy"]["partial_oco"] == (
        "resize_both_siblings_until_flat"
    )


def test_partial_state_is_recoverable_through_broker_reconciliation():
    broker = PaperBroker(10_000, profile=PROFILE, execution_proxy=_proxy())
    intent = BrokerOrderIntent(
        session_id="session-r",
        intent_id="intent-r",
        client_order_id="entry-r",
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        quantity="2.5",
        target="paper",
        price="100",
        stop_price="90",
        target_price="120",
    )
    broker.submit_entry(intent)
    broker.process_bar(
        ts_ms=T0,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=10,
    )

    report = broker.reconcile("session-r", [intent])

    assert report.safe_to_trade is True
    assert report.open_orders[0].client_order_id == "entry-r"
    assert report.open_orders[0].metadata["remaining_quantity"] == "1.5"
    assert report.positions[0].quantity == "1.0"
    assert report.metadata["ledger_balanced"] is True
