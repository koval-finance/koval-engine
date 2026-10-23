"""Chronological independent Grid lot accounting control."""

from decimal import Decimal

import pytest

from tests.engine.test_spot_multi_contract import api, session
from tests.engine.test_spot_multi_dca import closed, consume, find, reconcile


def test_grid_control_independent_bids_sell_then_later_rearm():
    spot = api()
    state = closed(session(), "11")
    assert len(find(state, "grid_buy")) == 2
    assert spot.spot_execution_report(state, mark="11")["free_quote"] == "10"
    bid = next(o for o in find(state, "grid_buy") if o.intent.limit_price == 10)
    state = consume(state, bid.intent.client_order_id, "10", "10")
    state = reconcile(state)
    ask = find(state, "grid_sell")[0]
    assert ask.intent.limit_price == 11
    assert ask.intent.quantity == 10
    state = consume(state, ask.intent.client_order_id, "10", "11")
    state = reconcile(state)
    assert len(find(state, "grid_buy")) == 2
    state = closed(state, "11", index=2, start=False)
    assert len(find(state, "grid_buy")) == 3
    report = spot.spot_execution_report(state, mark="11")
    assert report["quote_cash"] == "210"
    assert report["base_quantity"] == "0"
    assert report["quote_reserved"] == "190"
    assert report["free_quote"] == "20"
    assert report["realized_profit"] == "10"


def test_grid_terminal_partial_buy_sells_only_actual_net_quantity():
    state = closed(session(), "11")
    bid = next(o for o in find(state, "grid_buy") if o.intent.limit_price == 10)
    state = consume(state, bid.intent.client_order_id, "3", "10", "partial")
    assert find(state, "grid_sell") == []
    state = api().cancel_spot_order(
        state, bid.intent.client_order_id, command_id="manual", generation=0
    )
    state = consume(state, bid.intent.client_order_id, "2", "10", "canceled")
    state = reconcile(state)
    assert find(state, "grid_sell")[0].intent.quantity == 5


def test_normalized_duplicate_grid_levels_are_refused():
    from tests.engine.test_spot_multi_contract import binding, instrument, policy, recipe

    rules = instrument().model_copy(update={"price_tick": Decimal("1")})
    with pytest.raises(ValueError, match="duplicate"):
        api().create_spot_session(
            binding(rules=rules), recipe(lower_price="9", upper_price="9.5"), rules, policy()
        )
