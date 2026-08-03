import re

from koval.engine.client_order_id import make_client_order_id


def test_same_inputs_return_same_id_every_time():
    first = make_client_order_id("session-1", "intent-1", "binance_sandbox", "entry")
    second = make_client_order_id("session-1", "intent-1", "binance_sandbox", "entry")

    assert first == second


def test_role_changes_id():
    entry = make_client_order_id("session-1", "intent-1", "binance_sandbox", "entry")
    stop = make_client_order_id("session-1", "intent-1", "binance_sandbox", "stop")

    assert entry != stop


def test_id_is_venue_safe_ascii_and_short():
    order_id = make_client_order_id("session-1", "intent-1", "whitebit_sandbox", "entry")

    assert order_id.isascii()
    assert re.fullmatch(r"[A-Za-z0-9._-]+", order_id)
    assert len(order_id) <= 32


def test_id_does_not_leak_strategy_symbol_or_api_key_fragments():
    order_id = make_client_order_id(
        "strategy-BTC/USDT-api-key-abcd1234", "intent-1", "binance_sandbox", "entry"
    )

    assert "strategy" not in order_id
    assert "BTC" not in order_id
    assert "/" not in order_id
    assert "abcd1234" not in order_id


def test_rejects_too_short_max_length():
    try:
        make_client_order_id("s", "i", "v", "entry", max_len=15)
    except ValueError as exc:
        assert "max_len" in str(exc)
    else:
        raise AssertionError("expected ValueError")
