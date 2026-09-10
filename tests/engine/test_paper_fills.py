import pytest

from koval.engine.paper_fills import adjusted_price, commission, cost_split, required_margin


def test_buy_and_sell_adjust_in_the_adverse_direction():
    assert adjusted_price(100.0, side="buy", fraction=0.002) == pytest.approx(100.20)
    assert adjusted_price(85.0, side="sell", fraction=0.002) == pytest.approx(84.83)


def test_limit_caps_buys_and_floors_sells():
    assert adjusted_price(100.0, side="buy", fraction=0.002, limit=100.10) == pytest.approx(100.10)
    assert adjusted_price(100.0, side="sell", fraction=0.002, limit=99.95) == pytest.approx(99.95)
    assert adjusted_price(100.0, side="buy", fraction=0.002, limit=101.0) == pytest.approx(100.20)


def test_cost_split_is_pro_rata_and_zero_on_touch():
    spread, slip = cost_split(100.0, 100.20, 2.0, spread_bps=20.0, slippage_bps=10.0)
    assert spread == pytest.approx(0.20) and slip == pytest.approx(0.20)
    assert cost_split(100.0, 100.0, 2.0, spread_bps=20.0, slippage_bps=10.0) == (0.0, 0.0)
    assert cost_split(100.0, 100.10, 2.0, spread_bps=20.0, slippage_bps=10.0) == (
        pytest.approx(0.10),
        pytest.approx(0.10),
    )


def test_commission_and_margin():
    assert commission(2.0, 100.20, commission_bps=4.0) == pytest.approx(0.08016)
    assert required_margin(200.0, 100.0, leverage=5.0) == pytest.approx(4000.0)
    assert required_margin(-2.0, 100.0, leverage=1.0) == pytest.approx(200.0)
