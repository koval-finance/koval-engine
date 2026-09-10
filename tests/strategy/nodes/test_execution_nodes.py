import koval.strategy.nodes  # noqa: F401
from koval.strategy.graph.entities import OrderRequest, TradingIntent
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.registry import get_node


def _ctx():
    return BarContext(
        close=100.0,
        high=100.0,
        low=100.0,
        open=100.0,
        volume=1.0,
        bar_index=0,
        timestamp_ms=0,
        account_value=10_000.0,
    )


def test_order_constructor_builds_long_order():
    spec = get_node("exec.order_constructor")
    ev = spec.factory(spec.params_schema(sl_pct=2.0, risk_reward=2.0, risk_pct=1.0, leverage=1.0))
    intent = TradingIntent(bar_index=0, timestamp_ms=0, source_node_id="g", side="buy")
    out = ev(_ctx(), {"intent": [intent]}, {})
    order = out["order"]
    assert isinstance(order, OrderRequest)
    assert order.side == "buy"
    assert order.entry_price == 100.0
    assert order.stop_price < order.entry_price  # long stop below
    assert order.target_price > order.entry_price  # long target above
    assert order.quantity > 0


def test_order_constructor_no_intent_no_order():
    spec = get_node("exec.order_constructor")
    ev = spec.factory(spec.params_schema(sl_pct=2.0, risk_reward=2.0, risk_pct=1.0, leverage=1.0))
    out = ev(_ctx(), {"intent": []}, {})
    assert out["order"] is None


def test_order_pricing_fixed_percent_long():
    spec = get_node("exec.order_pricing")
    ev = spec.factory(spec.params_schema(sl_pct=2.0, risk_reward=2.0))
    intent = TradingIntent(bar_index=0, timestamp_ms=0, source_node_id="g", side="buy")
    out = ev(_ctx(), {"intent": [intent], "geometry": []}, {})
    o = out["order"]
    assert isinstance(o, OrderRequest)
    assert o.entry_price == 100.0
    assert o.stop_price < 100.0 and o.target_price > 100.0
    assert o.quantity == 0.0  # pricing does not size
    assert o.metadata["sl_model"] == "fixed_percent"


def test_order_pricing_structural_uses_geometry():
    from koval.strategy.graph.entities import MarketEvent

    spec = get_node("exec.order_pricing")
    ev = spec.factory(spec.params_schema(sl_model="structural"))
    intent = TradingIntent(bar_index=0, timestamp_ms=0, source_node_id="g", side="buy")
    geo = MarketEvent(
        bar_index=0, timestamp_ms=0, source_node_id="z", kind="ob", price_levels=[97.5, 90.0]
    )
    out = ev(_ctx(), {"intent": [intent], "geometry": [geo]}, {})
    assert out["order"].stop_price == 97.5


def test_order_pricing_no_intent_no_order():
    spec = get_node("exec.order_pricing")
    ev = spec.factory(spec.params_schema())
    assert ev(_ctx(), {"intent": [], "geometry": []}, {})["order"] is None


def test_order_pricing_normalizes_entry_model_to_public_order_type():
    spec = get_node("exec.order_pricing")
    intent = TradingIntent(bar_index=0, timestamp_ms=0, source_node_id="g", side="buy")

    limit_evaluator = spec.factory(spec.params_schema(entry_model="limit_at_zone"))
    stop_evaluator = spec.factory(spec.params_schema(entry_model="stop_market"))

    limit_order = limit_evaluator(_ctx(), {"intent": [intent], "geometry": []}, {})["order"]
    stop_order = stop_evaluator(_ctx(), {"intent": [intent], "geometry": []}, {})["order"]
    assert limit_order.order_type == "limit"
    assert limit_order.metadata["entry_model"] == "limit_at_zone"
    assert stop_order.order_type == "stop"
    assert stop_order.metadata["entry_model"] == "stop_market"


def test_position_sizing_sets_quantity_and_margin():
    spec = get_node("exec.position_sizing")
    ev = spec.factory(spec.params_schema(risk_pct=1.0, leverage=2.0, instrument_type="futures"))
    priced = OrderRequest(
        bar_index=0,
        timestamp_ms=0,
        source_node_id="p",
        symbol="BTCUSDT",
        side="buy",
        entry_price=100.0,
        stop_price=98.0,
        target_price=104.0,
        quantity=0.0,
    )
    out = ev(_ctx(), {"order": [priced]}, {})  # _ctx() has account_value=10_000
    o = out["order"]
    # Risk 1% of 10k = 100; distance 2 → size 50. Leverage reduces only
    # the required margin for that risk-limited position.
    assert o.quantity == 50.0
    assert o.quantity * (o.entry_price - o.stop_price) == 100.0
    assert o.metadata["required_margin"] == 50.0 * 100.0 / 2.0  # notional/leverage
    assert o.metadata["leverage"] == 2.0


def test_position_sizing_no_order_passes_none():
    spec = get_node("exec.position_sizing")
    ev = spec.factory(spec.params_schema())
    assert ev(_ctx(), {"order": []}, {})["order"] is None


def test_trade_risk_gate_allows_and_rejects():
    from koval.strategy.graph.entities import PolicyDecision

    spec = get_node("exec.trade_risk_gate")
    ev = spec.factory(spec.params_schema(min_rr=1.5, max_stop_distance_pct=10.0, max_leverage=20.0))
    good = OrderRequest(
        bar_index=0,
        timestamp_ms=0,
        source_node_id="s",
        symbol="X",
        side="buy",
        entry_price=100.0,
        stop_price=98.0,
        target_price=104.0,
        quantity=1.0,
        metadata={"leverage": 2.0},
    )
    d = ev(_ctx(), {"order": [good]}, {})["decision"]
    assert isinstance(d, PolicyDecision) and d.allowed and d.scope == "execution"

    bad = good.model_copy(update={"target_price": 100.5})  # rr < 1.5
    d2 = ev(_ctx(), {"order": [bad]}, {})["decision"]
    assert not d2.allowed and d2.reason


def _ctx_with_account(**account_kw):
    from koval.engine.account_state import AccountSnapshot

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
    base.update(account_kw)
    c = _ctx()
    c.account = AccountSnapshot(**base)
    return c


def _pf_order():
    return OrderRequest(
        bar_index=0,
        timestamp_ms=0,
        source_node_id="s",
        symbol="X",
        side="buy",
        entry_price=100.0,
        stop_price=98.0,
        quantity=1.0,
        metadata={"required_margin": 100.0},
    )


def test_portfolio_risk_gate_locks_out_on_daily_loss():
    spec = get_node("exec.portfolio_risk_gate")
    ev = spec.factory(spec.params_schema(max_daily_drawdown_pct=5.0))
    d = ev(_ctx_with_account(daily_loss_pct=6.0), {"order": [_pf_order()]}, {})["decision"]
    assert not d.allowed and "daily loss" in d.reason.lower()


def test_portfolio_risk_gate_allows_when_clear():
    spec = get_node("exec.portfolio_risk_gate")
    ev = spec.factory(spec.params_schema())
    d = ev(_ctx_with_account(), {"order": [_pf_order()]}, {})["decision"]
    assert d.allowed


def test_portfolio_risk_gate_no_account_blocks_safely():
    spec = get_node("exec.portfolio_risk_gate")
    ev = spec.factory(spec.params_schema())
    # _ctx() has account=None → cannot evaluate portfolio limits, allow by default
    d = ev(_ctx(), {"order": [_pf_order()]}, {})["decision"]
    assert d.allowed


def _approval(allowed):
    from koval.strategy.graph.entities import PolicyDecision

    return PolicyDecision(
        bar_index=0, timestamp_ms=0, source_node_id="g", allowed=allowed, scope="execution"
    )


def test_execution_router_routes_when_all_approved():
    spec = get_node("exec.execution_router")
    ev = spec.factory(spec.params_schema())
    order = OrderRequest(
        bar_index=0,
        timestamp_ms=0,
        source_node_id="s",
        symbol="X",
        side="buy",
        entry_price=100.0,
        stop_price=98.0,
        target_price=104.0,
        quantity=1.0,
    )
    out = ev(_ctx(), {"order": [order], "approvals": [_approval(True), _approval(True)]}, {})
    assert isinstance(out["order"], OrderRequest)
    assert out["order"].source_node_id == "execution_router"


def test_execution_router_blocks_when_any_rejected():
    spec = get_node("exec.execution_router")
    ev = spec.factory(spec.params_schema())
    order = OrderRequest(
        bar_index=0,
        timestamp_ms=0,
        source_node_id="s",
        symbol="X",
        side="buy",
        entry_price=100.0,
        stop_price=98.0,
        quantity=1.0,
    )
    out = ev(_ctx(), {"order": [order], "approvals": [_approval(True), _approval(False)]}, {})
    assert out["order"] is None


def test_execution_router_rejects_real_money_target():
    import pytest

    spec = get_node("exec.execution_router")
    with pytest.raises(ValueError):
        spec.params_schema(target="binance_live")


def test_execution_router_rejects_disabled_whitebit_target():
    import pytest

    spec = get_node("exec.execution_router")
    with pytest.raises(ValueError):
        spec.params_schema(target="whitebit_sandbox")


def test_execution_router_has_no_dead_host_target_parameter():
    spec = get_node("exec.execution_router")

    assert "target" not in spec.params_schema.model_fields


def test_execution_router_blocks_when_no_approvals():
    # An empty/unwired approvals port means no risk gate vouched for the order;
    # the router must refuse to route (all([]) would otherwise be True).
    spec = get_node("exec.execution_router")
    ev = spec.factory(spec.params_schema())
    order = OrderRequest(
        bar_index=0,
        timestamp_ms=0,
        source_node_id="s",
        symbol="X",
        side="buy",
        entry_price=100.0,
        stop_price=98.0,
        quantity=1.0,
    )
    out = ev(_ctx(), {"order": [order], "approvals": []}, {})
    assert out["order"] is None


def test_order_pricing_volatility_falls_back_on_short_history():
    import numpy as np

    spec = get_node("exec.order_pricing")
    ev = spec.factory(spec.params_schema(sl_model="volatility", sl_pct=2.0, atr_period=14))
    intent = TradingIntent(bar_index=0, timestamp_ms=0, source_node_id="g", side="buy")
    ctx = _ctx()
    # Only 2 bars → _atr returns 0 → must keep the fixed-percent fallback (sl=98),
    # NOT emit a zero-distance stop (sl == entry).
    ctx.highs = np.array([100.0, 101.0])
    ctx.lows = np.array([99.0, 99.5])
    ctx.closes = np.array([100.0, 100.0])
    o = ev(ctx, {"intent": [intent], "geometry": []}, {})["order"]
    assert o.stop_price == 98.0
    assert o.stop_price < o.entry_price


def test_position_sizing_fixed_notional():
    spec = get_node("exec.position_sizing")
    ev = spec.factory(
        spec.params_schema(
            sizing_model="fixed_notional", notional=1000.0, leverage=1.0, instrument_type="futures"
        )
    )
    priced = OrderRequest(
        bar_index=0,
        timestamp_ms=0,
        source_node_id="p",
        symbol="X",
        side="buy",
        entry_price=100.0,
        stop_price=98.0,
        quantity=0.0,
    )
    o = ev(_ctx(), {"order": [priced]}, {})["order"]
    assert o.quantity == 10.0  # notional 1000 / entry 100
    assert o.metadata["required_margin"] == 1000.0  # notional / leverage(1)


def test_position_sizing_spot_margin_is_full_notional():
    spec = get_node("exec.position_sizing")
    ev = spec.factory(spec.params_schema(risk_pct=1.0, leverage=5.0, instrument_type="spot"))
    priced = OrderRequest(
        bar_index=0,
        timestamp_ms=0,
        source_node_id="p",
        symbol="X",
        side="buy",
        entry_price=100.0,
        stop_price=98.0,
        quantity=0.0,
    )
    o = ev(_ctx(), {"order": [priced]}, {})["order"]
    # spot ignores leverage for margin → full notional
    assert o.metadata["required_margin"] == o.quantity * 100.0
