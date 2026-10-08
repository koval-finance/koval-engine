"""Spot quote cash is independent of the mark on purchased base inventory."""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

import numpy as np
import pytest

from koval.engine.account_ledger import AccountLedger
from koval.engine.account_state import PlatformAccountState
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.engine.paper_broker import PaperBroker, PaperOrderRejected
from koval.engine.paper_profile import PAPER_REALISTIC_VERSION, resolve_paper_profile
from koval.examples import parity_fixtures
from koval.strategy.graph.compat import compile_legacy

START = 1_704_067_200_000
STEP = 3_600_000
COSTS = {"commission_bps": 10, "spread_bps": 20, "slippage_bps": 10, "leverage": 1}
PROFILE = resolve_paper_profile({"version": PAPER_REALISTIC_VERSION, **COSTS})


@pytest.mark.parametrize("mark", [94, 100, 104])
def test_spot_account_mark_changes_equity_without_changing_quote_cash(mark):
    account = PlatformAccountState(10_000, market="spot")
    account.on_open(side="buy", entry_price=100.2, quantity=90, current_stop=90, margin=9018)
    account.on_fee(9.018)
    account.on_bar(equity=972.982 + 90 * mark, timestamp_ms=START)

    snapshot = account.snapshot()
    assert snapshot.balance == pytest.approx(9990.982)
    assert snapshot.equity == pytest.approx(972.982 + 90 * mark)
    assert snapshot.unrealized_pnl == pytest.approx(90 * (mark - 100.2))
    assert snapshot.free_margin == pytest.approx(972.982)
    assert account.reconcile().free_margin == pytest.approx(972.982)
    assert account.reconcile().balanced


@pytest.mark.parametrize("market", ["future", "futures", "usdm", "usd_m"])
@pytest.mark.parametrize("unrealized", [-558, 342])
def test_futures_account_keeps_marked_equity_availability(market, unrealized):
    account = PlatformAccountState(10_000, market=market)
    account.on_open(side="buy", entry_price=100.2, quantity=90, current_stop=90, margin=9018)
    account.on_fee(9.018)
    account.on_bar(equity=9990.982 + unrealized, timestamp_ms=START)

    assert account.snapshot().free_margin == pytest.approx(972.982 + unrealized)
    assert account.reconcile().free_margin == pytest.approx(972.982 + unrealized)
    assert account.reconcile().balanced


@pytest.mark.parametrize("unrealized", [-558, 342])
def test_default_account_and_ledger_keep_futures_availability(unrealized):
    account = PlatformAccountState(10_000)
    account.on_open(side="buy", entry_price=100.2, quantity=90, current_stop=90, margin=9018)
    account.on_fee(9.018)
    account.on_bar(equity=9990.982 + unrealized, timestamp_ms=START)

    report = account.ledger.reconcile(
        unrealized_pnl=unrealized, equity=9990.982 + unrealized, margin_used=9018
    )
    assert account.snapshot().free_margin == pytest.approx(972.982 + unrealized)
    assert account.reconcile() == report


@pytest.mark.parametrize("unrealized", [-558, 342])
def test_spot_ledger_reconciliation_uses_unspent_quote_cash(unrealized):
    ledger = AccountLedger(10_000)
    ledger.record(timestamp_ms=START, kind="commission", amount=-9.018)
    report = ledger.reconcile(
        unrealized_pnl=unrealized,
        equity=9990.982 + unrealized,
        margin_used=9018,
        market="spot",
    )
    assert report.free_margin == pytest.approx(972.982)
    assert report.balanced
    assert not ledger.reconcile(
        unrealized_pnl=unrealized,
        equity=9991.982 + unrealized,
        margin_used=9018,
        market="spot",
    ).balanced


@pytest.mark.parametrize("market", ["invalid", ""])
def test_account_and_reconciliation_reject_unknown_market(market):
    with pytest.raises(ValueError, match="unsupported market"):
        PlatformAccountState(10_000, market=market)
    with pytest.raises(ValueError, match="unsupported market"):
        AccountLedger(10_000).reconcile(
            unrealized_pnl=0, equity=10_000, margin_used=0, market=market
        )


def _partial_broker(*, market="spot", quantity=99.8, risk_budget=None):
    broker = PaperBroker(
        10_000,
        profile=PROFILE,
        execution_proxy=ExecutionProxyConfig(Decimal("0.01"), "carry", ExecutionLatency()),
        **({} if market is None else {"market": market}),
    )
    broker.submit_bracket(
        side="buy",
        entry_price=100,
        stop_price=90,
        target_price=120,
        quantity=quantity,
        risk_budget=risk_budget,
        order_type="market",
        client_order_id="partial-entry",
    )
    return broker


def _bar(broker, index, price, *, volume=9000):
    return broker.process_bar(
        ts_ms=START + index * STEP,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=volume,
    )


def test_spot_affordable_partial_remainder_fills_after_unrealized_loss():
    broker = _partial_broker(risk_budget=998)
    (first,) = _bar(broker, 1, 100)
    assert first.quantity == 90
    assert first.price == pytest.approx(100.2)
    assert broker.balance - broker.margin_used == pytest.approx(972.982)

    fills = _bar(broker, 2, 94)
    assert len(fills) == 1
    second = fills[0]
    assert second.quantity == pytest.approx(4.417985964075669)
    assert second.price == pytest.approx(94.188)
    assert second.quantity * second.price + second.commission == pytest.approx(416.537383246351)
    cash = 10_000 - sum(f.quantity * f.price + f.commission for f in (first, second))
    assert cash == pytest.approx(556.444616753649)
    assert broker.reconcile_ledger().free_margin == pytest.approx(cash)
    assert broker.reconcile_ledger().balanced
    assert broker.position.quantity == pytest.approx(first.quantity + second.quantity)
    assert broker.position.stop_price == 90
    assert broker.position.target_price == 120
    assert broker.consume_entry_rejection() is None
    assert not broker.pending

    # Protective partial exits sell only purchased inventory and release its cost.
    inventory = first.quantity + second.quantity
    for index in (3, 4):
        (exit_fill,) = _bar(broker, index, 85)
        assert exit_fill.kind == "stop_loss"
        inventory -= exit_fill.quantity
        cash += exit_fill.quantity * exit_fill.price - exit_fill.commission
        assert inventory >= 0
        assert cash >= 0
        assert broker.reconcile_ledger().free_margin == pytest.approx(cash)
        assert broker.reconcile_ledger().balanced
        assert (broker.position.quantity if broker.position else 0) == pytest.approx(inventory)
    assert broker.position is None


@pytest.mark.parametrize("market", [None, "future", "spot"])
def test_partial_remainder_affordability_preserves_futures_and_rejects_spending_spot_gains(market):
    broker = _partial_broker(quantity=99, market=market)
    (first,) = _bar(broker, 1, 100)
    fills = _bar(broker, 2, 110)
    if market == "spot":
        assert fills == []
        assert "insufficient_margin" in broker.consume_entry_rejection()
        assert broker.position.quantity == first.quantity == 90
        assert broker.reconcile_ledger().free_margin == pytest.approx(972.982)
    else:
        assert len(fills) == 1
        assert fills[0].quantity == 9
        assert broker.consume_entry_rejection() is None
        assert broker.reconcile_ledger().free_margin == pytest.approx(
            broker.equity - broker.margin_used
        )
    assert not broker.pending
    if market == "spot":
        assert broker.balance - broker.margin_used >= 0
    assert broker.reconcile_ledger().balanced


@pytest.mark.parametrize("quantity", [99.9, 100])
def test_spot_unaffordable_initial_purchase_preserves_cash_and_inventory(quantity):
    broker = PaperBroker(10_000, market="spot", profile=PROFILE)
    if quantity == 100:
        with pytest.raises(PaperOrderRejected, match="insufficient_margin"):
            broker.submit_bracket(
                side="buy",
                entry_price=100,
                stop_price=90,
                target_price=120,
                quantity=quantity,
                order_type="market",
            )
    else:
        broker.submit_bracket(
            side="buy",
            entry_price=100,
            stop_price=90,
            target_price=120,
            quantity=quantity,
            order_type="market",
        )
        assert _bar(broker, 1, 100) == []
        assert "insufficient_margin" in broker.consume_entry_rejection()
    assert broker.position is None
    assert not broker.pending
    assert broker.ledger.entries == ()
    assert broker.reconcile_ledger().free_margin == 10_000


@pytest.mark.parametrize("mark", [94, 100, 104])
def test_live_engine_spot_account_and_broker_reconcile_to_independent_cash(mark):
    fixture = next(f for f in parity_fixtures() if f["fixture_id"] == "long_gap_stop_v1")
    graph = compile_legacy(deepcopy(fixture["graph"]))
    order = next(b for b in graph["blocks"] if b["type"] == "exec.order_constructor")
    order["params"].update({"sl_pct": 10, "risk_reward": 50})
    rows = np.array(
        [
            [START + i * STEP, price, price + 1, price - 1, price, 1000]
            for i, price in enumerate([100, 100, mark, mark])
        ],
        dtype=float,
    )
    records = []
    engine = LiveEngine(
        graph,
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe="1h",
            initial_capital=10_000,
            market="spot",
            exchange="binance",
            execution={"version": PAPER_REALISTIC_VERSION, **COSTS},
            end_of_data_policy="mark_at_last_close",
        ),
        on_record=records.append,
    )
    engine.run(ReplayFeed(rows), StopSignal())

    (fill,) = [r["payload"] for r in records if r["kind"] == "fill"]
    quantity, price = float(fill["quantity"]), float(fill["price"])
    cash = 10_000 - quantity * price - fill["metadata"]["commission"]
    accounts = [r["payload"] for r in records if r["kind"] == "account_snapshot"]
    assert accounts[-1]["free_margin"] == pytest.approx(cash)
    assert accounts[-1]["equity"] == pytest.approx(cash + quantity * mark)
    for record in records:
        account = record["payload"].get("account") if record["kind"] == "decision" else None
        if account is not None:
            assert account["free_margin"] == pytest.approx(
                account["balance"] - account["margin_used"]
            )
    assert all(a["free_margin"] >= 0 for a in accounts)
    assert asdict(engine._account.reconcile()) == pytest.approx(
        asdict(engine._broker.reconcile_ledger()), rel=1e-12, abs=1e-9
    )
    assert engine._account.reconcile().free_margin == pytest.approx(cash)
    assert engine._account.reconcile().balanced
