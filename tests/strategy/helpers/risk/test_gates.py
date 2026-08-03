from koval.engine.account_state import AccountSnapshot
from koval.strategy.helpers.risk.gates import portfolio_risk_check, trade_risk_check


def _snap(**kw):
    base = dict(
        balance=10_000.0,
        equity=10_000.0,
        free_margin=10_000.0,
        margin_used=0.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        daily_pnl=0.0,
        peak_equity=10_000.0,
        drawdown_pct=0.0,
        open_positions=0,
        open_position=None,
    )
    base.update(kw)
    return AccountSnapshot(**base)


def test_trade_risk_allows_good_rr():
    # entry 100, stop 98 (dist 2), target 104 (dist 4) → rr 2.0 >= 1.5
    ok, reason = trade_risk_check(
        entry=100.0,
        stop=98.0,
        target=104.0,
        leverage=2.0,
        min_rr=1.5,
        max_stop_distance_pct=10.0,
        max_leverage=20.0,
    )
    assert ok and reason is None


def test_trade_risk_rejects_low_rr():
    ok, reason = trade_risk_check(
        entry=100.0,
        stop=98.0,
        target=101.0,
        leverage=2.0,
        min_rr=1.5,
        max_stop_distance_pct=10.0,
        max_leverage=20.0,
    )
    assert not ok and "rr" in reason.lower()


def test_trade_risk_rejects_wide_stop():
    ok, reason = trade_risk_check(
        entry=100.0,
        stop=80.0,
        target=140.0,
        leverage=2.0,
        min_rr=1.5,
        max_stop_distance_pct=10.0,
        max_leverage=20.0,
    )
    assert not ok and "stop" in reason.lower()


def test_trade_risk_rejects_over_leverage():
    ok, reason = trade_risk_check(
        entry=100.0,
        stop=98.0,
        target=104.0,
        leverage=50.0,
        min_rr=1.5,
        max_stop_distance_pct=10.0,
        max_leverage=20.0,
    )
    assert not ok and "leverage" in reason.lower()


def test_portfolio_rejects_on_daily_drawdown_lockout():
    ok, reason = portfolio_risk_check(
        account=_snap(drawdown_pct=6.0),
        required_margin=100.0,
        max_daily_drawdown_pct=5.0,
        max_concurrent_positions=1,
        max_total_margin_pct=50.0,
    )
    assert not ok and "drawdown" in reason.lower()


def test_portfolio_rejects_on_concurrent_cap():
    ok, reason = portfolio_risk_check(
        account=_snap(open_positions=1),
        required_margin=100.0,
        max_daily_drawdown_pct=5.0,
        max_concurrent_positions=1,
        max_total_margin_pct=50.0,
    )
    assert not ok and "concurrent" in reason.lower()


def test_portfolio_rejects_on_margin_cap():
    # used 0, new 6000 on 10k equity → 60% > 50%
    ok, reason = portfolio_risk_check(
        account=_snap(equity=10_000.0, margin_used=0.0),
        required_margin=6_000.0,
        max_daily_drawdown_pct=5.0,
        max_concurrent_positions=1,
        max_total_margin_pct=50.0,
    )
    assert not ok and "margin" in reason.lower()


def test_portfolio_allows_within_all_limits():
    ok, reason = portfolio_risk_check(
        account=_snap(),
        required_margin=100.0,
        max_daily_drawdown_pct=5.0,
        max_concurrent_positions=1,
        max_total_margin_pct=50.0,
    )
    assert ok and reason is None
