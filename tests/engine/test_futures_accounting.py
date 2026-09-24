"""Golden vectors for single-position linear Futures accounting."""

from decimal import Decimal

import pytest

from koval.engine.futures_accounting import (
    FuturesCashLedger,
    FuturesPosition,
    futures_account_snapshot,
)
from koval.engine.instrument_risk import (
    InstrumentSpecEvidence,
    MaintenanceMarginTier,
    MarkPriceRecord,
)

D = Decimal
T0 = 1_700_000_000_000


def spec(*, start: int = 0, end: int | None = None) -> InstrumentSpecEvidence:
    return InstrumentSpecEvidence(
        evidence_id="binance-btc-risk-v1",
        exchange="binance",
        market="future",
        canonical_symbol="BTCUSDT",
        effective_from_ms=start,
        effective_to_ms=end,
        tick_size=D("0.1"),
        step_size=D("0.001"),
        minimum_quantity=D("0.001"),
        minimum_notional=D("5"),
        minimum_price=D("0.1"),
        maximum_price=D("1000000"),
        contract_size=D("1"),
        collateral_currency="USDT",
        margin_tiers=(
            MaintenanceMarginTier(
                notional_floor=D("0"),
                notional_cap=None,
                maintenance_margin_rate=D("0.005"),
                maximum_leverage=D("20"),
            ),
        ),
        liquidation_fee_bps=D("50"),
        source="binance_usdm_risk",
        evidence_status="historical",
    )


def test_long_snapshot_reconciles_every_wallet_and_margin_term():
    snapshot = futures_account_snapshot(
        ledger=FuturesCashLedger(
            initial_wallet_balance=D("100"),
            realized_pnl=D("0"),
            trading_fees=D("1"),
            funding=D("-1"),
            liquidation_fees=D("0"),
        ),
        position=FuturesPosition(
            side="buy", quantity=D("10"), entry_price=D("100"), leverage=D("10")
        ),
        mark=MarkPriceRecord(T0, D("99"), "binance_mark"),
        instrument=spec(),
        evaluated_at_ms=T0,
        maximum_mark_age_ms=1_000,
    )

    assert snapshot.wallet_balance == D("98")
    assert snapshot.unrealized_pnl == D("-10")
    assert snapshot.equity == D("88")
    assert snapshot.margin_used == D("99")
    assert snapshot.available_balance == D("-11")
    assert snapshot.maintenance_margin == D("4.950")
    assert snapshot.estimated_liquidation_price.quantize(D("0.000001")) == D("90.653266")
    assert snapshot.liquidated is False
    assert snapshot.reconciled is True


def test_short_snapshot_uses_the_opposite_pnl_and_liquidation_direction():
    snapshot = futures_account_snapshot(
        ledger=FuturesCashLedger(
            initial_wallet_balance=D("100"),
            trading_fees=D("1"),
            funding=D("2"),
        ),
        position=FuturesPosition(
            side="sell", quantity=D("10"), entry_price=D("100"), leverage=D("10")
        ),
        mark=MarkPriceRecord(T0, D("101"), "binance_mark"),
        instrument=spec(),
        evaluated_at_ms=T0,
        maximum_mark_age_ms=1_000,
    )

    assert snapshot.wallet_balance == D("101")
    assert snapshot.unrealized_pnl == D("-10")
    assert snapshot.equity == D("91")
    assert snapshot.maintenance_margin == D("5.050")
    assert snapshot.estimated_liquidation_price.quantize(D("0.000001")) == D("109.552239")
    assert snapshot.liquidated is False


def test_mark_staleness_fails_closed():
    with pytest.raises(ValueError, match="mark-price evidence is stale"):
        futures_account_snapshot(
            ledger=FuturesCashLedger(initial_wallet_balance=D("100")),
            position=FuturesPosition(
                side="buy", quantity=D("1"), entry_price=D("100"), leverage=D("2")
            ),
            mark=MarkPriceRecord(T0, D("100"), "mark"),
            instrument=spec(),
            evaluated_at_ms=T0 + 1_001,
            maximum_mark_age_ms=1_000,
        )


def test_rule_snapshot_must_cover_the_evaluation_timestamp():
    with pytest.raises(ValueError, match="instrument evidence does not cover"):
        futures_account_snapshot(
            ledger=FuturesCashLedger(initial_wallet_balance=D("100")),
            position=None,
            mark=MarkPriceRecord(T0, D("100"), "mark"),
            instrument=spec(start=T0 + 1),
            evaluated_at_ms=T0,
            maximum_mark_age_ms=1_000,
        )


def test_leverage_above_notional_tier_cap_is_rejected():
    with pytest.raises(ValueError, match="maximum leverage 20"):
        futures_account_snapshot(
            ledger=FuturesCashLedger(initial_wallet_balance=D("100")),
            position=FuturesPosition(
                side="buy", quantity=D("1"), entry_price=D("100"), leverage=D("25")
            ),
            mark=MarkPriceRecord(T0, D("100"), "mark"),
            instrument=spec(),
            evaluated_at_ms=T0,
            maximum_mark_age_ms=1_000,
        )


def test_liquidated_snapshot_keeps_liquidation_fee_separate_from_trading_fees():
    snapshot = futures_account_snapshot(
        ledger=FuturesCashLedger(
            initial_wallet_balance=D("100"),
            trading_fees=D("1"),
            liquidation_fees=D("3"),
        ),
        position=FuturesPosition(
            side="buy", quantity=D("10"), entry_price=D("100"), leverage=D("10")
        ),
        mark=MarkPriceRecord(T0, D("90"), "mark"),
        instrument=spec(),
        evaluated_at_ms=T0,
        maximum_mark_age_ms=1_000,
    )

    assert snapshot.wallet_balance == D("96")
    assert snapshot.trading_fees == D("1")
    assert snapshot.liquidation_fees == D("3")
    assert snapshot.liquidated is True


def test_isolated_margin_limits_position_collateral_without_consuming_cross_wallet():
    snapshot = futures_account_snapshot(
        ledger=FuturesCashLedger(initial_wallet_balance=D("1000")),
        position=FuturesPosition(
            side="buy",
            quantity=D("1"),
            entry_price=D("100"),
            leverage=D("10"),
            margin_mode="isolated",
            isolated_margin=D("20"),
        ),
        mark=MarkPriceRecord(T0, D("85"), "mark"),
        instrument=spec(),
        evaluated_at_ms=T0,
        maximum_mark_age_ms=1_000,
    )

    assert snapshot.margin_mode == "isolated"
    assert snapshot.wallet_balance == D("1000")
    assert snapshot.equity == D("985")
    assert snapshot.margin_used == D("20")
    assert snapshot.available_balance == D("980")
    assert snapshot.position_margin_equity == D("5")
    assert snapshot.estimated_liquidation_price.quantize(D("0.000001")) == D("80.402010")
    assert snapshot.liquidated is False
    assert snapshot.reconciled is True


def test_isolated_margin_must_cover_initial_margin_and_fit_in_wallet():
    base = dict(
        side="buy",
        quantity=D("1"),
        entry_price=D("100"),
        leverage=D("10"),
        margin_mode="isolated",
    )
    with pytest.raises(ValueError, match="initial margin"):
        futures_account_snapshot(
            ledger=FuturesCashLedger(initial_wallet_balance=D("1000")),
            position=FuturesPosition(**base, isolated_margin=D("9")),
            mark=MarkPriceRecord(T0, D("100"), "mark"),
            instrument=spec(),
            evaluated_at_ms=T0,
            maximum_mark_age_ms=1_000,
        )
    with pytest.raises(ValueError, match="wallet balance"):
        futures_account_snapshot(
            ledger=FuturesCashLedger(initial_wallet_balance=D("1000")),
            position=FuturesPosition(**base, isolated_margin=D("1001")),
            mark=MarkPriceRecord(T0, D("100"), "mark"),
            instrument=spec(),
            evaluated_at_ms=T0,
            maximum_mark_age_ms=1_000,
        )
