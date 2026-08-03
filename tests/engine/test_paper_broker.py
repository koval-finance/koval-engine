import pytest

from koval.engine.broker import BrokerOrderIntent, ProtectiveOrderIntent
from koval.engine.paper_broker import PaperBroker


@pytest.mark.parametrize("starting_balance", [0, -1, float("nan"), float("inf")])
def test_constructor_rejects_non_positive_or_non_finite_balance(starting_balance):
    with pytest.raises(ValueError, match="starting balance"):
        PaperBroker(starting_balance=starting_balance)


def test_market_entry_fills_at_signal_bar_close_and_opens_position():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=2.0,
        order_type="market",
    )
    fill = b.fill_market_if_pending(ts_ms=0, price=100.0)
    assert fill is not None and fill.kind == "entry" and fill.price == 100.0
    assert b.position is not None and b.position.quantity == 2.0
    assert b.pending is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": 0.0},
        {"quantity": float("nan")},
        {"entry_price": -1.0},
        {"side": "hold"},
        {"order_type": "iceberg"},
        {"stop_price": 105.0},
        {"target_price": 95.0},
    ],
)
def test_submit_bracket_rejects_invalid_order(overrides):
    arguments = {
        "side": "buy",
        "entry_price": 100.0,
        "stop_price": 95.0,
        "target_price": 110.0,
        "quantity": 1.0,
        "order_type": "market",
    }
    arguments.update(overrides)
    broker = PaperBroker(starting_balance=10_000.0)

    with pytest.raises(ValueError, match="paper order"):
        broker.submit_bracket(**arguments)

    assert broker.pending is False


def test_long_take_profit_fills_when_high_crosses_target():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=2.0,
        order_type="market",
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)
    fills = b.process_bar(ts_ms=60_000, open=101, high=111, low=100, close=109)
    assert len(fills) == 1 and fills[0].kind == "take_profit"
    assert fills[0].realized_pnl == (110.0 - 100.0) * 2.0
    assert b.position is None
    assert b.balance == 10_000.0 + 20.0


def test_long_stop_loss_fills_when_low_crosses_stop():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=2.0,
        order_type="market",
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)
    fills = b.process_bar(ts_ms=60_000, open=99, high=100, low=94, close=96)
    assert fills[0].kind == "stop_loss"
    assert fills[0].realized_pnl == (95.0 - 100.0) * 2.0
    assert b.balance == 10_000.0 - 10.0


def test_long_stop_gap_fills_at_worse_bar_open():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=2.0,
        order_type="market",
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)

    fills = b.process_bar(ts_ms=60_000, open=90.0, high=96.0, low=89.0, close=92.0)

    assert fills[0].kind == "stop_loss"
    assert fills[0].price == 90.0
    assert fills[0].realized_pnl == -20.0


def test_short_stop_gap_fills_at_worse_bar_open():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="sell",
        entry_price=100.0,
        stop_price=105.0,
        target_price=90.0,
        quantity=2.0,
        order_type="market",
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)

    fills = b.process_bar(ts_ms=60_000, open=110.0, high=111.0, low=104.0, close=108.0)

    assert fills[0].kind == "stop_loss"
    assert fills[0].price == 110.0
    assert fills[0].realized_pnl == -20.0


def test_stop_entry_gap_fills_at_bar_open():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=105.0,
        stop_price=95.0,
        target_price=120.0,
        quantity=1.0,
        order_type="stop",
    )

    fills = b.process_bar(ts_ms=60_000, open=110.0, high=112.0, low=109.0, close=111.0)

    assert [fill.kind for fill in fills] == ["entry"]
    assert fills[0].price == 110.0


def test_ambiguous_same_bar_cross_assumes_stop_first():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=1.0,
        order_type="market",
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)
    # bar spans both 95 and 110 -> conservative: stop first
    fills = b.process_bar(ts_ms=60_000, open=100, high=111, low=94, close=108)
    assert fills[0].kind == "stop_loss"


def test_limit_entry_fills_when_price_trades_through_on_later_bar():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=98.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=1.0,
        order_type="limit_at_zone",
    )
    assert b.fill_market_if_pending(ts_ms=0, price=100.0) is None  # not a market order
    fills = b.process_bar(ts_ms=60_000, open=100, high=101, low=97, close=99)
    assert len(fills) == 1 and fills[0].kind == "entry" and fills[0].price == 98.0
    assert b.position is not None


def test_limit_entry_stop_loss_is_applied_on_the_entry_bar():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=1.0,
        order_type="limit",
    )

    fills = b.process_bar(ts_ms=60_000, open=101, high=111, low=94, close=108)

    assert [fill.kind for fill in fills] == ["entry", "stop_loss"]
    assert b.position is None
    assert b.balance == 9_995.0


def test_limit_entry_take_profit_is_applied_on_the_entry_bar():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=1.0,
        order_type="limit",
    )

    fills = b.process_bar(ts_ms=60_000, open=101, high=111, low=99, close=108)

    assert [fill.kind for fill in fills] == ["entry", "take_profit"]
    assert b.position is None
    assert b.balance == 10_010.0


@pytest.mark.parametrize("order_type", ["limit", "limit_at_zone"])
def test_limit_order_names_share_the_same_fill_rule(order_type):
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=98.0,
        stop_price=90.0,
        target_price=110.0,
        quantity=1.0,
        order_type=order_type,
    )

    fills = b.process_bar(ts_ms=60_000, open=100, high=101, low=97, close=99)

    assert [fill.kind for fill in fills] == ["entry"]
    assert fills[0].price == 98.0


def test_limit_entry_marks_equity_to_the_entry_bar_close():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=98.0,
        stop_price=90.0,
        target_price=110.0,
        quantity=2.0,
        order_type="limit",
    )

    b.process_bar(ts_ms=60_000, open=100, high=105, low=97, close=104)

    assert b.position is not None
    assert b.equity == 10_012.0


@pytest.mark.parametrize("order_type", ["stop", "stop_market"])
def test_stop_order_names_share_the_same_fill_rule(order_type):
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=105.0,
        stop_price=95.0,
        target_price=115.0,
        quantity=1.0,
        order_type=order_type,
    )

    fills = b.process_bar(ts_ms=60_000, open=100, high=106, low=99, close=105)

    assert [fill.kind for fill in fills] == ["entry"]
    assert fills[0].price == 105.0


def test_equity_marks_open_position_to_close():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=3.0,
        order_type="market",
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)
    b.process_bar(ts_ms=60_000, open=101, high=105, low=100, close=104)  # no exit
    assert b.position is not None
    assert b.equity == 10_000.0 + (104.0 - 100.0) * 3.0


def test_modify_stop_then_trailing_stop_fills():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=120.0,
        quantity=1.0,
        order_type="market",
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)
    b.modify_stop(102.0)
    fills = b.process_bar(ts_ms=60_000, open=104, high=105, low=101, close=103)
    assert fills[0].kind == "stop_loss" and fills[0].price == 102.0


def test_modify_stop_rejects_increased_risk():
    broker = PaperBroker(starting_balance=10_000.0)
    broker.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=120.0,
        quantity=1.0,
        order_type="market",
    )
    broker.fill_market_if_pending(ts_ms=0, price=100.0)

    with pytest.raises(ValueError, match="tighten"):
        broker.modify_stop(94.0)

    assert broker.position is not None
    assert broker.position.stop_price == 95.0


def test_flatten_closes_open_position_at_price():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="sell",
        entry_price=100.0,
        stop_price=105.0,
        target_price=90.0,
        quantity=2.0,
        order_type="market",
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)
    fill = b.flatten(ts_ms=60_000, price=97.0)
    assert fill is not None and fill.kind == "manual_close"
    assert fill.realized_pnl == (100.0 - 97.0) * 2.0  # short profit
    assert b.position is None


def test_submit_entry_accepts_broker_intent_and_preserves_client_id():
    b = PaperBroker(starting_balance=10_000.0)
    intent = BrokerOrderIntent(
        session_id="s1",
        intent_id="i1",
        client_order_id="kv-entry",
        symbol="BTCUSDT",
        side="buy",
        order_type="market",
        quantity="1.0",
        price="100.0",
        stop_price="95.0",
        target_price="110.0",
        target="paper",
    )

    ack = b.submit_entry(intent)

    assert ack.status == "accepted"
    assert ack.client_order_id == "kv-entry"
    assert b.pending is True


def test_poll_fills_returns_broker_fill_after_legacy_market_fill():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_entry(
        BrokerOrderIntent(
            session_id="s1",
            intent_id="i1",
            client_order_id="kv-entry",
            symbol="BTCUSDT",
            side="buy",
            order_type="market",
            quantity="1.0",
            price="100.0",
            stop_price="95.0",
            target_price="110.0",
            target="paper",
        )
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)

    fills = b.poll_fills("s1")

    assert len(fills) == 1
    assert fills[0].client_order_id == "kv-entry"
    assert fills[0].status == "filled"
    assert fills[0].role == "entry"
    assert b.poll_fills("s1") == []


def test_place_protection_updates_existing_stop_and_target():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=1.0,
        order_type="market",
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)

    acks = b.place_protection(
        ProtectiveOrderIntent(
            session_id="s1",
            entry_client_order_id="kv-entry",
            stop_client_order_id="kv-stop",
            target_client_order_id="kv-target",
            symbol="BTCUSDT",
            side="buy",
            quantity="1.0",
            stop_price="98.0",
            target_price="120.0",
            target="paper",
        )
    )

    assert {ack.client_order_id for ack in acks} == {"kv-stop", "kv-target"}
    assert b.position is not None
    assert b.position.stop_price == 98.0
    assert b.position.target_price == 120.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"target": "binance_sandbox"},
        {"side": "sell"},
        {"quantity": "2.0"},
        {"stop_price": "90.0"},
        {"stop_price": "101.0"},
        {"target_price": "99.0"},
        {"reduce_only": False},
        {"stop_client_order_id": ""},
        {"target_client_order_id": "kv-stop"},
    ],
)
def test_place_protection_rejects_invalid_or_risk_increasing_intent(overrides):
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_entry(
        BrokerOrderIntent(
            session_id="s1",
            intent_id="i1",
            client_order_id="kv-entry",
            symbol="BTCUSDT",
            side="buy",
            order_type="market",
            quantity="1.0",
            price="100.0",
            stop_price="95.0",
            target_price="110.0",
            target="paper",
        )
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)
    values = {
        "session_id": "s1",
        "entry_client_order_id": "kv-entry",
        "stop_client_order_id": "kv-stop",
        "target_client_order_id": "kv-target",
        "symbol": "BTCUSDT",
        "side": "buy",
        "quantity": "1.0",
        "stop_price": "98.0",
        "target_price": "120.0",
        "target": "paper",
    }
    values.update(overrides)

    with pytest.raises(ValueError, match="protective order"):
        b.place_protection(ProtectiveOrderIntent(**values))

    assert b.position is not None
    assert b.position.stop_price == 95.0
    assert b.position.target_price == 110.0


def test_reconcile_cancel_all_and_protocol_flatten_are_safe_for_paper():
    b = PaperBroker(starting_balance=10_000.0)
    report = b.reconcile("s1", [])
    assert report.safe_to_trade is True

    b.submit_bracket(
        side="buy",
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        quantity=1.0,
        order_type="limit_at_zone",
    )
    cancel_acks = b.cancel_all("BTCUSDT", "s1")
    assert cancel_acks[0].status == "canceled"
    assert b.pending is False

    assert b.flatten("BTCUSDT", "s1") is None


def test_protocol_flatten_closes_at_last_seen_price_and_records_fill():
    b = PaperBroker(starting_balance=10_000.0)
    b.submit_entry(
        BrokerOrderIntent(
            session_id="s1",
            intent_id="i1",
            client_order_id="kv-entry",
            symbol="BTCUSDT",
            side="buy",
            order_type="market",
            quantity="1.0",
            price="100.0",
            stop_price="95.0",
            target_price="110.0",
            target="paper",
        )
    )
    b.fill_market_if_pending(ts_ms=0, price=100.0)
    b.process_bar(ts_ms=60_000, open=101, high=105, low=100, close=104)

    ack = b.flatten("BTCUSDT", "s1")

    assert ack is not None
    assert ack.status == "filled"
    assert ack.client_order_id == "paper-flatten-s1"
    assert b.position is None
    assert b.balance == 10_004.0
    fills = b.poll_fills("s1")
    assert [fill.role for fill in fills] == ["entry", "flatten"]
    assert fills[-1].price == "104.0"
    assert fills[-1].timestamp_ms == 60_000
