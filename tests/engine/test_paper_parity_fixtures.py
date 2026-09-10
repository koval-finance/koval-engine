"""The paper broker must reproduce every public parity fixture exactly."""

import numpy as np
import pytest

from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.examples import parity_fixtures


@pytest.mark.parametrize("fixture", parity_fixtures(), ids=lambda f: f["fixture_id"])
def test_paper_reproduces_parity_fixture(fixture):
    if fixture.get("requires_direct_setup"):
        pytest.skip(
            "covered by test_live_engine.py::"
            "test_paper_insufficient_margin_emits_order_rejected_and_keeps_trading"
        )
    trades, status = [], {}
    LiveEngine(
        fixture["graph"],
        LiveEngineConfig(
            symbol="BTCUSDT",
            timeframe=fixture["timeframe"],
            initial_capital=fixture["capital"],
            execution={
                "version": fixture.get("profile_version", "paper_ohlcv_fixed_v1"),
                **fixture["costs"],
            },
        ),
        on_trade=trades.append,
        on_status=status.update,
    ).run(ReplayFeed(np.array(fixture["candles"], dtype=float), delay_seconds=0.0), StopSignal())
    expected = fixture["expected"]
    strategy_trades = [t for t in trades if t["reason"] != "manual"]
    assert len(strategy_trades) == len(expected["trades"])
    for trade, want in zip(strategy_trades, expected["trades"], strict=True):
        assert trade["direction"] == want["direction"]
        assert trade["entry_price"] == pytest.approx(want["entry_price"], abs=1e-9)
        assert trade["exit_price"] == pytest.approx(want["exit_price"], abs=1e-9)
        assert trade["commission"] == pytest.approx(want["commission"], abs=1e-9)
        assert trade["reason"] == want["exit_reason"]
    assert status["metrics"]["equity"] == pytest.approx(expected["final_equity"], abs=1e-6)
    if "ambiguity_reason" in expected:
        assert (
            status["execution_profile"]["ambiguities"][0]["reason_code"]
            == expected["ambiguity_reason"]
        )
