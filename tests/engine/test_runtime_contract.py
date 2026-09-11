"""Cross-runtime regressions for the 0.11 runtime contract."""

import numpy as np
import pytest

from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.engine.paper_broker import PaperBroker
from tests.engine.test_live_engine import _SingleEntryStrategy

EXECUTION = {
    "version": "paper_ohlcv_realistic_v2",
    "commission_bps": 0.0,
    "spread_bps": 0.0,
    "slippage_bps": 0.0,
}


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_dynamic_target_is_applied_after_the_decision_bar(monkeypatch, side):
    class Strategy(_SingleEntryStrategy):
        def should_long(self):
            return side == "buy" and self.open_count == 0

        def should_short(self):
            return side == "sell" and self.open_count == 0

        def go_short(self):
            return self._setup

        def on_tp_update(self, trade_id):
            return 105.0 if side == "buy" else 95.0

    strategy = Strategy(
        direction="long" if side == "buy" else "short",
        stop_loss=90.0 if side == "buy" else 110.0,
        take_profit=120.0 if side == "buy" else 80.0,
    )
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    trades, statuses = [], []
    engine = LiveEngine(
        {},
        LiveEngineConfig("BTCUSDT", "1m", 10_000, execution=EXECUTION),
        on_trade=trades.append,
        on_status=statuses.append,
    )
    engine.run(
        ReplayFeed(
            np.array(
                [
                    [0, 100, 101, 99, 100, 10],
                    [60_000, 100, 106, 94, 100, 10],
                    [120_000, 100, 106, 94, 100, 10],
                ],
                dtype=float,
            )
        ),
        StopSignal(),
    )
    assert len(trades) == 1
    assert trades[0]["reason"] == "tp"
    assert trades[0]["exit_time"] == "1970-01-01T00:02:00Z"
    assert trades[0]["pnl"] == pytest.approx(5)
    assert statuses[1]["open_position"]["take_profit"] == (105 if side == "buy" else 95)


def _open_broker(side="buy"):
    broker = PaperBroker(10_000)
    broker.submit_bracket(
        side=side,
        entry_price=100,
        stop_price=90 if side == "buy" else 110,
        target_price=120 if side == "buy" else 80,
        quantity=1,
        order_type="market",
    )
    broker.fill_market_if_pending(ts_ms=0, price=100)
    return broker


def test_both_protection_legs_validate_against_the_new_bracket():
    broker = _open_broker()
    broker.modify_protection(stop_price=121, target_price=130)
    assert broker.position.stop_price == 121
    assert broker.position.target_price == 130


@pytest.mark.parametrize("stop,target", [(89, 130), (95, 94), (95, float("nan")), (95, 0)])
def test_invalid_replacement_changes_neither_leg(stop, target):
    broker = _open_broker()
    before = broker.position
    with pytest.raises(ValueError):
        broker.modify_protection(stop_price=stop, target_price=target)
    assert broker.position == before


def test_spot_short_is_rejected_and_later_long_can_trade(monkeypatch):
    class Strategy(_SingleEntryStrategy):
        def should_long(self):
            return self.bar_index > 1 and self.open_count == 0

        def should_short(self):
            return self.bar_index == 1

        def go_short(self):
            from koval.strategy.base.trade_setup import TradeSetup

            return TradeSetup("short", 100, 110, 90, size=1, entry_type="market")

    strategy = Strategy()
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda graph: strategy)
    events, trades = [], []
    engine = LiveEngine(
        {},
        LiveEngineConfig("BTCUSDT", "1m", 10_000, market="spot", execution=EXECUTION),
        on_event=events.append,
        on_trade=trades.append,
    )
    engine.run(
        ReplayFeed(
            np.array(
                [
                    [0, 100, 101, 99, 100, 10],
                    [60_000, 100, 101, 99, 100, 10],
                    [120_000, 100, 111, 99, 110, 10],
                ],
                dtype=float,
            )
        ),
        StopSignal(),
    )
    rejections = [event for event in events if event["event_type"] == "ORDER_REJECTED"]
    assert rejections[0]["payload"]["reason"] == "spot_short_unsupported"
    assert trades[0]["direction"] == "long"
