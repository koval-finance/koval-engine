"""Graphs and lifecycle hooks must observe actual runtime account state."""

from decimal import Decimal as D

import numpy as np
import pytest

from koval.engine.account_state import PlatformAccountState
from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.engine.funding import FundingRecord, build_funding_series
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.strategy.base.trade_setup import TradeSetup
from koval.strategy.graph.strategy import build_graph_strategy
from tests.engine.test_runtime_contract import EXECUTION

GRAPH = {"blocks": [{"id": "clock", "type": "fact.every_bar", "params": {}}], "connections": []}


def test_public_account_binding_does_not_book_requested_hooks_twice():
    strategy = build_graph_strategy(GRAPH)()
    assert callable(getattr(strategy, "bind_account", None))
    account = PlatformAccountState(1000)
    strategy.bind_account(account.snapshot)
    account.on_open(side="buy", entry_price=101, quantity=1, current_stop=90, margin=101)
    account.on_fee(1)
    strategy.on_open_position(1, TradeSetup("long", 100, 90, 120, 2))
    strategy.account_value = 999
    assert strategy._ctx().account == account.snapshot()
    account.on_close(realized_pnl=5)
    strategy.on_close_position(1, {"pnl": 5})
    assert strategy._ctx().account.balance == 1004
    assert strategy.account_snapshot() == account.snapshot()


def test_runtime_graph_reads_fills_fees_funding_and_partial_exposure(monkeypatch):
    captured, opened, closed = [], [], []

    class Probe(build_graph_strategy(GRAPH)):
        def should_long(self):
            return self.bar_index == 1

        def go_long(self):
            return TradeSetup("long", 100, 90, 120, 2, "market")

        def on_bar(self):
            super().on_bar()
            captured.append(self._ctx().account)

        def on_open_position(self, trade_id, setup):
            super().on_open_position(trade_id, setup)
            opened.append(setup)

        def on_close_position(self, trade_id, result):
            super().on_close_position(trade_id, result)
            closed.append(self.account_snapshot())

    strategy = Probe()
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda _: strategy)
    funding = build_funding_series(
        [
            FundingRecord("BTCUSDT", D("0.001"), i * 60_000, D("100"), 60_000, "test")
            for i in range(4)
        ],
        exchange="binance",
        market="future",
        symbol="BTCUSDT",
        requested_start_ms=0,
        requested_end_ms=180_000,
    )
    statuses = []
    engine = LiveEngine(
        GRAPH,
        LiveEngineConfig(
            "BTCUSDT",
            "1m",
            10_000,
            execution=EXECUTION | {"commission_bps": 4},
            funding=funding,
            execution_proxy=ExecutionProxyConfig(D("1"), "carry", ExecutionLatency()),
        ),
        on_status=statuses.append,
    )
    rows = np.array(
        [
            [0, 100, 101, 99, 100, 10],
            [60_000, 101, 102, 100, 101, 1],
            [120_000, 101, 102, 100, 101, 1],
            [180_000, 119, 126, 118, 125, 1],
        ]
    )
    engine.run(ReplayFeed(rows), StopSignal())
    assert opened[0].entry_price == 101
    assert opened[0].size == 1
    for snapshot, status in zip(captured, statuses, strict=False):
        for name in ("balance", "equity", "fees", "funding", "unrealized_pnl", "realized_pnl"):
            assert getattr(snapshot, name) == pytest.approx(status["metrics"][name])
        assert snapshot.margin_used == pytest.approx(status["margin_used"])
        position = status["open_position"]
        if position:
            assert snapshot.open_position.quantity == pytest.approx(position["quantity"])
            assert snapshot.open_position.entry_price == pytest.approx(position["entry_price"])
    assert captured[-1].funding < 0
    assert 0 < captured[-1].open_position.quantity < 1
    assert closed[-1].open_position is None
    assert closed[-1].balance == pytest.approx(engine._broker.balance)
