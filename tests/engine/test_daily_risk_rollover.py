"""First-bar executions must remain visible to the new UTC day's risk gates."""

from dataclasses import replace

import numpy as np
import pytest

from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup
from tests.engine.spot_fixtures import ScriptedSpotBroker, entry, execution, report
from tests.engine.test_spot_testnet import spot_graph

DAY = 86_400_000
EXECUTION = {
    "version": "paper_ohlcv_fixed_v1",
    "commission_bps": 10.0,
    "spread_bps": 0.0,
    "slippage_bps": 0.0,
}


class _DailyRiskStrategy(DeclarativeStrategy):
    def __init__(self, *, repeat=False):
        super().__init__()
        self.repeat = repeat
        self.open_count = 0

    def should_long(self):
        return self.repeat or self.open_count == 0

    def go_long(self):
        return TradeSetup("long", self.close, self.close - 10, self.close + 20, 10, "market")

    def filters(self):
        return [lambda: self.account_snapshot().daily_loss_pct < 0.5]

    def was_blocked(self):
        return False

    def on_open_position(self, trade_id, setup):
        self.open_count += 1


@pytest.mark.parametrize("market", ["spot", "future"])
@pytest.mark.parametrize(
    ("exit_open", "expected_balance", "expected_daily_pnl"),
    [(80.0, 9798.2, -250.8), (125.0, 10247.75, 198.75)],
)
def test_first_bar_gap_exit_preserves_previous_close_daily_baseline(
    monkeypatch, market, exit_open, expected_balance, expected_daily_pnl
):
    strategy = _DailyRiskStrategy()
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda _: strategy)
    statuses = []
    engine = LiveEngine(
        {},
        LiveEngineConfig("BTCUSDT", "1m", 10_000, market=market, execution=EXECUTION),
        on_status=statuses.append,
    )
    engine.run(
        ReplayFeed(
            np.array(
                [
                    [DAY - 120_000, 100, 101, 99, 100, 10],
                    [DAY - 60_000, 100, 106, 99, 105, 10],
                    [DAY, exit_open, exit_open + 1, exit_open - 1, exit_open, 10],
                ]
            )
        ),
        StopSignal(),
    )

    # Ten units gained 50 before midnight, less the one-unit quote entry fee.
    assert statuses[1]["metrics"]["equity"] == pytest.approx(10049.0)
    metrics = statuses[-1]["metrics"]
    assert metrics["balance"] == pytest.approx(expected_balance)
    assert metrics["daily_pnl"] == pytest.approx(expected_daily_pnl)
    assert metrics["daily_loss_pct"] == pytest.approx(max(0.0, -expected_daily_pnl / 10049.0 * 100))
    assert strategy.account_snapshot().daily_pnl == pytest.approx(expected_daily_pnl)


def test_first_bar_loss_prevents_reentry_through_daily_risk_filter(monkeypatch):
    strategy = _DailyRiskStrategy(repeat=True)
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda _: strategy)
    intents, events = [], []
    engine = LiveEngine(
        {},
        LiveEngineConfig("BTCUSDT", "1m", 10_000, market="spot", execution=EXECUTION),
        on_order_intent=intents.append,
        on_event=events.append,
    )
    engine.run(
        ReplayFeed(
            np.array(
                [
                    [DAY - 120_000, 100, 101, 99, 100, 10],
                    [DAY - 60_000, 100, 101, 99, 100, 10],
                    [DAY, 80, 81, 79, 80, 10],
                ]
            )
        ),
        StopSignal(),
    )

    assert len(intents) == 1
    assert any(event["event_type"] == "FILTER_REJECTED" for event in events)
    assert strategy.account_snapshot().daily_loss_pct > 0.5


def test_midnight_spot_loss_survives_delivery_of_the_preceding_closed_candle():
    broker = ScriptedSpotBroker()
    statuses = []
    engine = LiveEngine(
        spot_graph(),
        LiveEngineConfig("BTCUSDT", "1m", 1000, exchange="binance", market="spot"),
        broker=broker,
        session_id="session",
        on_record=lambda record: None,
        on_status=statuses.append,
    )
    engine.reconcile_spot()
    engine._spot.submit(entry())
    broker.events = [replace(execution(), timestamp_ms=DAY - 120_000)]
    broker.reports = [report("800", "2")]
    engine.watch_orders()
    protection = engine.spot_account["protection"]
    broker.events = [
        replace(
            execution(
                "midnight-stop",
                role="stop",
                client_id=protection["stop_client_order_id"],
                price="90",
            ),
            timestamp_ms=DAY,
        )
    ]
    broker.reports = [report("980", "0")]

    # The 23:59 candle arrives after the midnight fill is polled.
    engine.run(
        ReplayFeed(
            np.array(
                [
                    [DAY - 60_000, 100, 101, 99, 100, 10],
                    [DAY, 90, 91, 89, 90, 10],
                ]
            )
        ),
        StopSignal(),
    )

    for status in statuses:
        assert status["metrics"]["equity"] == pytest.approx(980)
        assert status["metrics"]["daily_pnl"] == pytest.approx(-20)
        assert status["metrics"]["daily_loss_pct"] == pytest.approx(2)
