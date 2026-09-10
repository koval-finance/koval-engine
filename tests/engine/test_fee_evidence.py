import pytest

from koval.engine.fee_evidence import FeeScheduleEvidence, resolve_fee_application
from koval.engine.paper_broker import PaperBroker
from koval.engine.paper_profile import PAPER_REALISTIC_VERSION, resolve_paper_profile

PROFILE = resolve_paper_profile(
    {
        "version": PAPER_REALISTIC_VERSION,
        "commission_bps": 9.0,
        "spread_bps": 0.0,
        "slippage_bps": 0.0,
    }
)


def test_historical_fee_evidence_preserves_role_currency_source_and_discount():
    schedule = FeeScheduleEvidence(
        evidence_id="binance-vip0-2024",
        maker_bps=2.0,
        taker_bps=4.0,
        currency="USDT",
        evidence_status="historical",
        source="archived_account_commission_response",
        effective_from_ms=100,
        effective_to_ms=200,
        discount_treatment="bnb_discount_not_applied",
        tier_id="VIP0",
    )

    application = resolve_fee_application(schedule, role="maker", timestamp_ms=150)

    assert application.rate_bps == 2.0
    assert application.currency == "USDT"
    assert application.evidence_id == "binance-vip0-2024"
    assert application.tier_id == "VIP0"
    assert application.evidence_status == "historical"
    assert application.discount_treatment == "bnb_discount_not_applied"


def test_current_snapshot_used_outside_its_time_is_labeled_approximation():
    schedule = FeeScheduleEvidence(
        evidence_id="binance-current",
        maker_bps=2.0,
        taker_bps=4.0,
        currency="USDT",
        evidence_status="current_snapshot",
        source="current_public_schedule",
        effective_from_ms=1_000,
    )

    application = resolve_fee_application(schedule, role="taker", timestamp_ms=500)

    assert application.rate_bps == 4.0
    assert application.evidence_status == "approximation"


def test_maker_rebate_is_preserved_as_a_negative_fee_rate():
    schedule = FeeScheduleEvidence(
        evidence_id="maker-rebate",
        maker_bps=-1.0,
        taker_bps=4.0,
        currency="USDT",
        evidence_status="historical",
        source="archived_account_fee_response",
        effective_from_ms=0,
        effective_to_ms=100,
    )
    application = resolve_fee_application(schedule, role="maker", timestamp_ms=0)
    assert application.rate_bps == -1.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"effective_from_ms": None},
        {"effective_to_ms": None},
        {"effective_from_ms": True},
        {"effective_to_ms": -1},
    ],
)
def test_historical_fee_evidence_requires_a_valid_bounded_interval(overrides):
    values = {
        "evidence_id": "historical-fee",
        "maker_bps": 2.0,
        "taker_bps": 4.0,
        "currency": "USDT",
        "evidence_status": "historical",
        "source": "archived_account_fee_response",
        "effective_from_ms": 0,
        "effective_to_ms": 100,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match="fee"):
        FeeScheduleEvidence(**values)


@pytest.mark.parametrize("role", ["unknown", ""])
def test_unknown_fee_role_is_rejected(role):
    schedule = FeeScheduleEvidence(
        evidence_id="configured",
        maker_bps=2.0,
        taker_bps=4.0,
        currency="USDT",
        evidence_status="approximation",
        source="config",
    )
    with pytest.raises(ValueError, match="maker or taker"):
        resolve_fee_application(schedule, role=role, timestamp_ms=0)


def test_paper_fills_and_ledger_preserve_fee_evidence():
    schedule = FeeScheduleEvidence(
        evidence_id="venue-tier-3",
        maker_bps=1.0,
        taker_bps=5.0,
        currency="USDT",
        evidence_status="historical",
        source="archived_account_fee_response",
        effective_from_ms=100,
        effective_to_ms=300,
        discount_treatment="token_discount_applied",
    )
    broker = PaperBroker(10_000.0, profile=PROFILE, fee_schedule=schedule)
    broker.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=90.0,
        target_price=110.0,
        quantity=2.0,
        order_type="limit",
        client_order_id="entry-1",
    )

    entry, take_profit = broker.process_bar(
        ts_ms=150,
        open=101.0,
        high=111.0,
        low=99.0,
        close=105.0,
    )

    assert entry.liquidity_role == "maker"
    assert entry.commission == pytest.approx(2 * 100 * 1 / 10_000)
    assert take_profit.liquidity_role == "taker"
    assert take_profit.commission == pytest.approx(2 * 110 * 5 / 10_000)
    assert entry.fee_currency == take_profit.fee_currency == "USDT"
    assert entry.fee_evidence_id == take_profit.fee_evidence_id == "venue-tier-3"
    assert entry.fee_evidence_status == take_profit.fee_evidence_status == "historical"
    assert broker.resolved_metadata["fee_evidence"]["evidence_id"] == "venue-tier-3"
    commissions = [item for item in broker.ledger.entries if item.kind == "commission"]
    assert [item.metadata["liquidity_role"] for item in commissions] == ["maker", "taker"]
    assert all(item.metadata["source"] == "archived_account_fee_response" for item in commissions)
