"""Run public direct-setup/hook fixtures through the live runtime."""

import numpy as np
import pytest

from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.examples import parity_fixtures
from tests.engine.test_live_engine import _SingleEntryStrategy


@pytest.mark.parametrize(
    "fixture_id", ["long_dynamic_target_v2", "short_dynamic_target_v2", "spot_short_rejected_v2"]
)
def test_runtime_fixture(fixture_id, monkeypatch):
    matches = [fixture for fixture in parity_fixtures() if fixture["fixture_id"] == fixture_id]
    assert len(matches) == 1
    fixture = matches[0]
    side = fixture["setup"]["direction"]

    class Strategy(_SingleEntryStrategy):
        def should_long(self):
            return side == "long" and self.open_count == 0

        def should_short(self):
            return side == "short" and self.open_count == 0

        def go_short(self):
            return self._setup

        def on_tp_update(self, trade_id):
            actions = fixture.get("strategy_hooks", {}).get("on_tp_update", [])
            return next(
                (
                    action["value"]
                    for action in actions
                    if action["after_bar_index"] == self.bar_index - 1
                ),
                None,
            )

    strategy = Strategy(**fixture["setup"])
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda _: strategy)
    trades, events, status = [], [], {}
    engine = LiveEngine(
        fixture["graph"],
        LiveEngineConfig(
            "BTCUSDT",
            fixture["timeframe"],
            fixture["capital"],
            exchange=fixture["exchange"],
            market=fixture["exchange_type"],
            execution={"version": fixture["profile_version"], **fixture["costs"]},
        ),
        on_trade=trades.append,
        on_event=events.append,
        on_status=status.update,
    )
    engine.run(ReplayFeed(np.array(fixture["candles"], dtype=float)), StopSignal())
    assert len(trades) == len(fixture["expected"]["trades"])
    for trade, expected in zip(trades, fixture["expected"]["trades"], strict=True):
        assert trade["reason"] == expected["exit_reason"]
        for key in ("entry_price", "exit_price", "commission", "pnl"):
            assert trade[key] == pytest.approx(expected[key])
    assert status["metrics"]["equity"] == pytest.approx(fixture["expected"]["final_equity"])
    if "rejection_reason" in fixture["expected"]:
        assert any(
            event["event_type"] == "ORDER_REJECTED"
            and event["payload"]["reason"] == fixture["expected"]["rejection_reason"]
            for event in events
        )
        assert status["entries_halted"] is False
