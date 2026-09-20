"""Regressions found by execution and evidence review for 0.11."""

from decimal import Decimal

import numpy as np
import pytest

from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.engine.market_data import build_candle_dataset
from koval.engine.paper_broker import PaperBroker
from koval.engine.paper_profile import resolve_paper_profile
from tests.engine.live_fixtures import ema_cross_graph
from tests.engine.test_instrument_risk import _spec
from tests.engine.test_runtime_contract import EXECUTION, _open_broker


@pytest.mark.parametrize(
    "change",
    [
        {"requested_start_ms": 1},
        {"requested_end_ms": 59_999},
        {"requested_end_ms": -60_000},
        {"exchange": "unknown"},
        {"source": ""},
        {"candles": np.array([[0, 100, 101, 99, 100, 1], [60_000, 100, 101, 99, 100, 1]])},
    ],
)
def test_dataset_rejects_mislabelled_or_out_of_range_evidence(change):
    params = dict(
        candles=np.array([[0, 100, 101, 99, 100, 1]]),
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        timeframe="1m",
        requested_start_ms=0,
        requested_end_ms=60_000,
        source="test",
    )
    with pytest.raises(ValueError):
        build_candle_dataset(**(params | change))


@pytest.mark.parametrize(
    "latency", [ExecutionLatency(cancellation_ms=1), ExecutionLatency(replacement_ms=1)]
)
def test_unmodelled_mutation_latency_is_refused(latency):
    proxy = ExecutionProxyConfig(Decimal("0.1"), "carry", latency)
    with pytest.raises(ValueError, match="cancellation.*replacement"):
        PaperBroker(10_000, profile=resolve_paper_profile(EXECUTION), execution_proxy=proxy)


def test_contract_multiplier_cannot_corrupt_base_quantity_accounting():
    with pytest.raises(ValueError, match="contract_size"):
        PaperBroker(
            10_000,
            profile=resolve_paper_profile(EXECUTION),
            instrument_specs=(_spec(contract_size=Decimal("10")),),
        )


def test_funding_cannot_silently_end_before_the_run():
    from koval.engine.funding import build_funding_series

    funding = build_funding_series(
        [],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=0,
        requested_end_ms=60_000,
        interval_ms=28_800_000,
        settlement_anchor_ms=3_600_000,
        schedule_source="archived_schedule",
    )
    broker = PaperBroker(10_000, profile=resolve_paper_profile(EXECUTION), funding=funding)
    with pytest.raises(ValueError, match="funding.*cover"):
        broker.process_bar(ts_ms=120_000, open=100, high=101, low=99, close=100)


def test_injected_broker_must_match_venue_even_when_no_trade_occurs():
    from koval.engine.funding import build_funding_series

    funding = build_funding_series(
        [],
        exchange="whitebit",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=0,
        requested_end_ms=60_000,
        interval_ms=28_800_000,
        settlement_anchor_ms=3_600_000,
        schedule_source="archived_schedule",
    )
    broker = PaperBroker(10_000, funding=funding)
    with pytest.raises(ValueError, match="exchange"):
        LiveEngine(
            ema_cross_graph(),
            LiveEngineConfig("BTCUSDT", "1m", 10_000, exchange="binance"),
            broker=broker,
        )


@pytest.mark.parametrize("target_update", [None, 130])
def test_partial_remainder_preserves_profit_lock_and_new_target(monkeypatch, target_update):
    from tests.engine.test_live_engine import _SingleEntryStrategy

    class Strategy(_SingleEntryStrategy):
        def on_sl_update(self, trade_id):
            return 103

        def on_tp_update(self, trade_id):
            return target_update

    strategy = Strategy(size=2, stop_loss=90, take_profit=120)
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda _: strategy)
    proxy = ExecutionProxyConfig(Decimal("1"), "carry", ExecutionLatency())
    statuses = []
    engine = LiveEngine(
        {},
        LiveEngineConfig("BTCUSDT", "1m", 10_000, execution=EXECUTION, execution_proxy=proxy),
        on_status=statuses.append,
    )
    engine.run(
        ReplayFeed(
            np.array(
                [
                    [0, 100, 101, 99, 100, 10],
                    [60_000, 100, 106, 99, 105, 1],
                    [120_000, 105, 106, 104, 105, 1],
                ],
                dtype=float,
            )
        ),
        StopSignal(),
    )
    assert statuses[2]["open_position"]["current_stop"] == 103
    assert statuses[2]["open_position"]["take_profit"] == (target_update or 120)


@pytest.mark.parametrize("stop_only", [False, True])
def test_replacing_protection_revalidates_tick_size(stop_only):
    broker = _open_broker()
    broker._instrument_specs = (_spec(start=0, end=60_000, tick_size=Decimal("1")),)
    if stop_only:
        broker.modify_stop(95.2)
    else:
        broker.modify_protection(stop_price=95.2, target_price=109.2)
    assert broker.position.stop_price == 95
    assert broker.position.target_price == (120 if stop_only else 109)


def test_stop_update_is_preserved_when_entry_remainder_fills():
    proxy = ExecutionProxyConfig(Decimal("1"), "carry", ExecutionLatency())
    broker = PaperBroker(10_000, profile=resolve_paper_profile(EXECUTION), execution_proxy=proxy)
    broker.submit_bracket(
        side="buy",
        entry_price=100,
        stop_price=90,
        target_price=120,
        quantity=2,
        order_type="market",
        decision_timestamp_ms=0,
    )
    broker.process_bar(ts_ms=0, open=100, high=101, low=99, close=100, volume=1)
    assert broker.pending
    broker.modify_stop(95)
    broker.process_bar(ts_ms=60_000, open=100, high=101, low=94, close=100, volume=2)
    assert broker.position is None


def test_trade_costs_include_every_partial_exit(monkeypatch):
    from koval.engine.paper_broker import Fill
    from tests.engine.test_live_engine import _FakeSetup, _SingleEntryStrategy

    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda _: _SingleEntryStrategy()
    )
    engine = LiveEngine({}, LiveEngineConfig("BTCUSDT", "1m", 10_000))
    engine._open_entry_fill = Fill("entry", "buy", 100, 2, 0, 0, spread_cost=1, slippage_cost=2)
    engine._account.on_open(side="buy", entry_price=100, quantity=2, current_stop=90, margin=0)
    engine._on_partial_close(
        Fill("take_profit", "buy", 110, 1, 60_000, 10, spread_cost=3, slippage_cost=4)
    )
    record = engine._trade_record(
        _FakeSetup(),
        Fill("take_profit", "buy", 110, 1, 120_000, 10, spread_cost=5, slippage_cost=6),
    )
    assert record["execution_costs"]["spread_cost"] == 9
    assert record["execution_costs"]["slippage_cost"] == 12


def test_non_quote_fees_are_not_deducted_as_quote_money():
    from koval.engine.fee_evidence import FeeScheduleEvidence

    broker = PaperBroker(
        10_000,
        profile=resolve_paper_profile(EXECUTION),
        fee_schedule=FeeScheduleEvidence("fees", 1, 4, "BNB", "approximation", "test"),
    )
    with pytest.raises(ValueError, match="fee currency"):
        broker.submit_bracket(
            side="buy",
            entry_price=100,
            stop_price=90,
            target_price=120,
            quantity=1,
            order_type="market",
            symbol="BTCUSDT",
        )
    assert broker.balance == 10_000
    assert not broker.pending


def test_legacy_profile_cannot_silently_ignore_supplied_fees():
    from koval.engine.fee_evidence import FeeScheduleEvidence

    with pytest.raises(ValueError, match="fee.*costed"):
        PaperBroker(
            10_000, fee_schedule=FeeScheduleEvidence("fees", 1, 4, "quote", "approximation", "test")
        )


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_partial_opening_entry_margin_does_not_use_later_close(side):
    from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig

    outcomes = []
    for close in (50, 100, 150):
        broker = PaperBroker(
            200,
            profile=resolve_paper_profile(
                {
                    "version": "paper_ohlcv_realistic_v2",
                    "commission_bps": 0,
                    "spread_bps": 0,
                    "slippage_bps": 0,
                }
            ),
            execution_proxy=ExecutionProxyConfig(Decimal("0.1"), "carry", ExecutionLatency()),
        )
        broker.submit_bracket(
            side=side,
            entry_price=100,
            stop_price=10 if side == "buy" else 300,
            target_price=300 if side == "buy" else 10,
            quantity=2,
            order_type="market",
            decision_timestamp_ms=0,
        )
        broker.process_bar(ts_ms=60_000, open=100, high=101, low=99, close=100, volume=10)
        fills = broker.process_bar(
            ts_ms=120_000,
            open=100,
            high=151,
            low=49,
            close=close,
            volume=10,
        )
        outcomes.append([(fill.kind, fill.price, fill.quantity) for fill in fills])
    assert outcomes == [[("entry", 100, 1)]] * 3


def test_protection_delay_starts_when_delayed_limit_actually_fills():
    from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig

    broker = PaperBroker(
        1000,
        profile=resolve_paper_profile(
            {
                "version": "paper_ohlcv_realistic_v2",
                "commission_bps": 0,
                "spread_bps": 0,
                "slippage_bps": 0,
            }
        ),
        execution_proxy=ExecutionProxyConfig(
            Decimal("1"),
            "carry",
            ExecutionLatency(protection_activation_ms=120_000),
        ),
    )
    broker.submit_bracket(
        side="buy",
        entry_price=100,
        stop_price=90,
        target_price=120,
        quantity=1,
        order_type="limit",
        decision_timestamp_ms=0,
    )
    for timestamp in (0, 60_000):
        assert (
            broker.process_bar(
                ts_ms=timestamp,
                open=110,
                high=111,
                low=109,
                close=110,
                volume=10,
            )
            == []
        )
    fills = broker.process_bar(ts_ms=120_000, open=100, high=105, low=89, close=100, volume=10)
    assert [fill.kind for fill in fills] == ["entry"]
    assert fills[0].protection_active_timestamp_ms == 240_000
    assert broker.process_bar(ts_ms=180_000, open=100, high=105, low=89, close=100, volume=10) == []
    assert (
        broker.process_bar(
            ts_ms=240_000,
            open=100,
            high=105,
            low=89,
            close=100,
            volume=10,
        )[0].kind
        == "stop_loss"
    )


def test_live_runtime_refuses_funding_inside_execution_candle():
    from koval.engine.funding import FundingRecord, build_funding_series

    funding = build_funding_series(
        [
            FundingRecord("BTCUSDT", Decimal(".01"), t, Decimal("100"), 60_000, "test")
            for t in (30_000, 90_000)
        ],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=0,
        requested_end_ms=120_000,
    )
    with pytest.raises(ValueError, match="funding.*execution.*grid"):
        LiveEngine(
            ema_cross_graph(),
            LiveEngineConfig(
                symbol="BTCUSDT",
                timeframe="1m",
                initial_capital=1000,
                exchange="binance",
                execution=EXECUTION,
                funding=funding,
            ),
        )


def test_paper_context_rejects_non_quote_collateral_before_execution():
    with pytest.raises(ValueError, match="quote collateral"):
        PaperBroker(
            10000,
            profile=resolve_paper_profile(EXECUTION),
            exchange="binance",
            instrument_specs=(_spec(collateral_currency="BTC"),),
        )
