from decimal import Decimal

import pytest

from koval.engine.instrument_risk import (
    InstrumentSpecEvidence,
    MaintenanceMarginTier,
    MarkPriceRecord,
    build_mark_price_series,
    evaluate_liquidation,
    normalize_order,
    select_instrument_spec,
)
from koval.engine.paper_broker import PaperBroker
from koval.engine.paper_profile import PAPER_REALISTIC_VERSION, resolve_paper_profile

T0 = 1_700_000_000_000
HOUR = 3_600_000


def _spec(*, start=T0, end=T0 + 10 * HOUR, **overrides):
    values = dict(
        evidence_id="binance-btc-tier-v1",
        exchange="binance",
        market="future",
        canonical_symbol="BTCUSDT",
        effective_from_ms=start,
        effective_to_ms=end,
        tick_size=Decimal("0.10"),
        step_size=Decimal("0.01"),
        minimum_quantity=Decimal("0.10"),
        minimum_notional=Decimal("5"),
        minimum_price=Decimal("1"),
        maximum_price=Decimal("1000000"),
        contract_size=Decimal("1"),
        collateral_currency="USDT",
        margin_tiers=(
            MaintenanceMarginTier(
                notional_floor=Decimal("0"),
                notional_cap=Decimal("50000"),
                maintenance_margin_rate=Decimal("0.005"),
                maintenance_amount=Decimal("0"),
            ),
        ),
        liquidation_fee_bps=Decimal("50"),
        source="archived_exchange_info_and_leverage_bracket",
        evidence_status="historical",
    )
    values.update(overrides)
    return InstrumentSpecEvidence(**values)


def test_order_normalization_uses_the_spec_valid_at_simulation_time():
    spec = _spec()

    normalized = normalize_order(
        spec,
        side="buy",
        order_type="limit",
        quantity=Decimal("1.239"),
        price=Decimal("100.127"),
    )

    assert normalized.quantity == Decimal("1.23")
    assert normalized.price == Decimal("100.10")
    assert normalized.spec_evidence_id == spec.evidence_id
    assert select_instrument_spec((spec,), timestamp_ms=T0 + HOUR) is spec
    with pytest.raises(ValueError, match="no instrument evidence"):
        select_instrument_spec((spec,), timestamp_ms=T0 - 1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"margin_mode": "isolated"},
        {"liquidation_fee_bps": Decimal("10000")},
        {
            "margin_tiers": (
                MaintenanceMarginTier(
                    notional_floor=Decimal("100"),
                    notional_cap=Decimal("200"),
                    maintenance_margin_rate=Decimal("0.01"),
                ),
                MaintenanceMarginTier(
                    notional_floor=Decimal("0"),
                    notional_cap=Decimal("100"),
                    maintenance_margin_rate=Decimal("0.005"),
                ),
            )
        },
    ],
)
def test_instrument_evidence_rejects_unsupported_or_ambiguous_risk_rules(overrides):
    with pytest.raises(ValueError, match="instrument|margin|liquidation"):
        _spec(**overrides)


def test_order_below_venue_minimum_is_rejected_after_quantization():
    with pytest.raises(ValueError, match="minimum notional"):
        normalize_order(
            _spec(),
            side="buy",
            order_type="limit",
            quantity=Decimal("0.10"),
            price=Decimal("10"),
        )


def test_order_outside_historical_reference_price_band_is_rejected():
    with pytest.raises(ValueError, match="price band"):
        normalize_order(
            _spec(
                price_band_low_multiplier=Decimal("0.8"),
                price_band_high_multiplier=Decimal("1.2"),
            ),
            side="buy",
            order_type="limit",
            quantity=Decimal("1"),
            price=Decimal("121"),
            reference_price=Decimal("100"),
        )


def test_liquidation_uses_mark_price_and_maintenance_tier():
    state = evaluate_liquidation(
        _spec(),
        side="buy",
        quantity=Decimal("100"),
        entry_price=Decimal("100"),
        cash_balance=Decimal("1000"),
        mark_price=Decimal("90"),
    )

    assert state.equity == Decimal("0")
    assert state.maintenance_margin == Decimal("45.000")
    assert state.liquidated is True


def test_mark_price_series_requires_complete_interval_coverage():
    records = (
        MarkPriceRecord(T0, Decimal("100"), "binance-premium-index"),
        MarkPriceRecord(T0 + HOUR, Decimal("99"), "binance-premium-index"),
    )
    series = build_mark_price_series(
        records,
        exchange="binance",
        symbol="BTCUSDT",
        interval_ms=HOUR,
        requested_start_ms=T0,
        requested_end_ms=T0 + HOUR,
    )
    assert series.coverage_complete is True
    with pytest.raises(ValueError, match="missing mark price"):
        build_mark_price_series(
            records[:1],
            exchange="binance",
            symbol="BTCUSDT",
            interval_ms=HOUR,
            requested_start_ms=T0,
            requested_end_ms=T0 + HOUR,
        )


def test_paper_broker_liquidates_from_mark_price_and_reconciles_fee():
    marks = build_mark_price_series(
        (
            MarkPriceRecord(T0, Decimal("100"), "binance-premium-index"),
            MarkPriceRecord(T0 + HOUR, Decimal("90"), "binance-premium-index"),
        ),
        exchange="binance",
        symbol="BTCUSDT",
        interval_ms=HOUR,
        requested_start_ms=T0,
        requested_end_ms=T0 + HOUR,
    )
    profile = resolve_paper_profile(
        {
            "version": PAPER_REALISTIC_VERSION,
            "commission_bps": 0.0,
            "spread_bps": 0.0,
            "slippage_bps": 0.0,
            "leverage": 10.0,
        }
    )
    broker = PaperBroker(
        1_000.0,
        profile=profile,
        instrument_specs=(_spec(),),
        mark_prices=marks,
    )
    broker.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=80.0,
        target_price=120.0,
        quantity=100.0,
        order_type="market",
        client_order_id="entry-liq",
        symbol="BTCUSDT",
    )
    broker.process_bar(ts_ms=T0, open=100, high=101, low=99, close=100)

    (liquidation,) = broker.process_bar(
        ts_ms=T0 + HOUR,
        open=95,
        high=96,
        low=94,
        close=95,
    )

    assert liquidation.kind == "liquidation"
    assert liquidation.reference_price == 90.0
    assert liquidation.liquidation_fee == pytest.approx(45.0)
    assert broker.balance == pytest.approx(-45.0)
    assert [entry.kind for entry in broker.ledger.entries] == [
        "commission",
        "trade_pnl",
        "liquidation_fee",
    ]
    assert broker.reconcile_ledger().balanced is True


def test_paper_broker_rejects_mismatched_instrument_and_mark_price_evidence():
    marks = build_mark_price_series(
        (MarkPriceRecord(T0, Decimal("100"), "binance-premium-index"),),
        exchange="binance",
        symbol="ETHUSDT",
        interval_ms=HOUR,
        requested_start_ms=T0,
        requested_end_ms=T0,
    )

    with pytest.raises(ValueError, match="evidence symbol mismatch"):
        PaperBroker(
            1_000.0,
            profile=resolve_paper_profile(
                {
                    "version": PAPER_REALISTIC_VERSION,
                    "commission_bps": 0.0,
                    "spread_bps": 0.0,
                    "slippage_bps": 0.0,
                }
            ),
            instrument_specs=(_spec(),),
            mark_prices=marks,
        )


def test_mark_price_lookup_does_not_scan_the_series():
    """The paper broker calls ``at`` once per bar for the life of a position, so
    a linear scan turns a routine backtest into a quadratic one: 30 days of
    one-minute marks cost ~40s of pure lookup before this was indexed."""
    import time

    count, interval = 20_000, 60_000
    series = build_mark_price_series(
        [MarkPriceRecord(T0 + index * interval, Decimal("100"), "probe") for index in range(count)],
        exchange="binance",
        symbol="BTCUSDT",
        interval_ms=interval,
        requested_start_ms=T0,
        requested_end_ms=T0 + (count - 1) * interval,
    )

    started = time.perf_counter()
    for index in range(count):
        assert series.at(T0 + index * interval) is not None
    elapsed = time.perf_counter() - started

    assert series.at(T0 + 1) is None
    assert elapsed < 1.5, f"one lookup per bar must stay constant-time, took {elapsed:.1f}s"
