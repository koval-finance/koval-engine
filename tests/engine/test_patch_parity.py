"""Execution regressions reported by the independent 0.11 plugin review."""

from decimal import Decimal as D

import pytest

from koval.engine.execution_proxy import ExecutionLatency, ExecutionProxyConfig
from koval.engine.paper_broker import PaperBroker
from koval.engine.paper_profile import resolve_paper_profile
from tests.engine.test_instrument_risk import _spec
from tests.engine.test_runtime_contract import EXECUTION


def _broker(*, side="buy", instrument=False, volume_cap=None, costs=None, risk=20):
    broker = PaperBroker(
        10_000,
        profile=resolve_paper_profile(EXECUTION | (costs or {})),
        instrument_specs=((_spec(start=0, end=600_000, step_size=D("0.1")),) if instrument else ()),
        execution_proxy=(
            ExecutionProxyConfig(D(str(volume_cap)), "carry", ExecutionLatency())
            if volume_cap is not None
            else None
        ),
    )
    broker.submit_bracket(
        side=side,
        entry_price=100,
        stop_price=90 if side == "buy" else 110,
        target_price=120 if side == "buy" else 80,
        quantity=2,
        order_type="market",
        symbol="BTCUSDT",
        risk_budget=risk,
    )
    return broker


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_partial_exit_marks_remaining_exposure_at_the_bar_close(side):
    broker = _broker(side=side, volume_cap=1, costs={"slippage_bps": 10})
    broker.process_bar(ts_ms=0, open=100, high=101, low=99, close=100, volume=10)
    close = 125 if side == "buy" else 75
    fills = broker.process_bar(
        ts_ms=60_000, open=100, high=max(101, close), low=min(99, close), close=close, volume=1
    )
    assert fills[0].status == "partial"
    position = broker.position
    sign = 1 if side == "buy" else -1
    expected = broker.balance + sign * position.quantity * (close - position.entry_price)
    assert broker.equity == pytest.approx(expected)


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("volume_cap", [None, 0.137])
def test_final_entry_quantity_obeys_step_after_risk_and_volume_caps(side, volume_cap):
    broker = _broker(
        side=side,
        instrument=True,
        volume_cap=volume_cap,
        costs={"commission_bps": 4, "slippage_bps": 10},
    )
    fill = broker.process_bar(ts_ms=0, open=100, high=101, low=99, close=100, volume=10)[0]
    assert D(str(fill.quantity)) % D("0.1") == 0
    assert fill.quantity == pytest.approx(1.9 if volume_cap is None else 1.3)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_risk_clipping_leaves_unused_volume_for_same_bar_protection(side):
    broker = _broker(side=side, volume_cap=0.2, costs={"commission_bps": 4})
    fills = broker.process_bar(ts_ms=0, open=100, high=111, low=89, close=100, volume=10)
    assert [fill.kind for fill in fills] == ["entry", "stop_loss"]
    assert sum(fill.quantity for fill in fills) == pytest.approx(2)


def test_risk_cap_below_minimum_quantity_rejects_without_filling():
    broker = _broker(instrument=True, risk=0.5)
    fills = broker.process_bar(ts_ms=0, open=100, high=101, low=99, close=100, volume=10)
    assert fills == []
    assert broker.position is None
    assert not broker.pending
    assert "instrument_constraint" in broker.consume_entry_rejection()


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_partial_exit_volume_cap_keeps_quantity_on_step(side):
    broker = _broker(side=side, instrument=True, volume_cap=1)
    broker.process_bar(ts_ms=0, open=100, high=101, low=99, close=100, volume=10)
    fills = broker.process_bar(ts_ms=60_000, open=100, high=121, low=79, close=100, volume=1.37)
    assert fills[0].quantity == pytest.approx(1.3)
    # The final residual must close without leaving floating-point lot dust.
    fills = broker.process_bar(ts_ms=120_000, open=100, high=121, low=79, close=100, volume=10)
    assert fills[0].quantity == pytest.approx(0.7)
    assert broker.position is None


@pytest.mark.parametrize("gap", [85, 125, 100])
def test_runtime_accepts_broker_matched_entry_and_same_bar_exit(monkeypatch, gap):
    import numpy as np

    from koval.engine.live_engine import LiveEngine, LiveEngineConfig
    from koval.engine.live_feed import ReplayFeed, StopSignal
    from tests.engine.test_live_engine import _SingleEntryStrategy

    strategy = _SingleEntryStrategy(stop_loss=90, take_profit=120, size=2)
    monkeypatch.setattr("koval.engine.live_engine.assemble_from_graph", lambda _: strategy)
    trades, statuses, observed_fills = [], [], []
    proxy = ExecutionProxyConfig(D("0.2"), "carry", ExecutionLatency()) if gap == 100 else None
    engine = LiveEngine(
        {},
        LiveEngineConfig(
            "BTCUSDT",
            "1m",
            10_000,
            execution=EXECUTION | {"commission_bps": 4},
            execution_proxy=proxy,
        ),
        on_trade=trades.append,
        on_status=statuses.append,
        on_fill=observed_fills.append,
    )
    rows = np.array([[0, 100, 101, 99, 100, 10], [60_000, gap, gap + 1, min(gap - 1, 89), gap, 10]])
    engine.run(ReplayFeed(rows), StopSignal())
    assert len(trades) == 1
    assert statuses[-1]["open_position"] is None
    assert statuses[-1]["metrics"]["equity"] == pytest.approx(engine._broker.equity)
    assert any(fill["role"] in {"stop", "target"} for fill in observed_fills)
