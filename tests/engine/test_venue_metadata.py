import pytest

from koval.engine.broker import BrokerOrderIntent
from koval.engine.venue_metadata import VenueSymbolMetadata, validate_order_against_metadata


def _intent(**overrides):
    data = {
        "session_id": "session-1",
        "intent_id": "intent-1",
        "client_order_id": "kv-entry",
        "symbol": "BTCUSDT",
        "side": "buy",
        "order_type": "limit",
        "quantity": "0.100",
        "target": "binance_sandbox",
        "price": "100.05",
    }
    data.update(overrides)
    return BrokerOrderIntent(**data)


def _metadata(**overrides):
    data = {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "price_tick": "0.01",
        "quantity_step": "0.001",
        "min_qty": "0.001",
        "min_notional": "5",
        "allowed_order_types": ("market", "limit", "stop_market", "stop_limit", "limit_at_zone"),
        "price_precision": 2,
        "quantity_precision": 3,
    }
    data.update(overrides)
    return VenueSymbolMetadata(**data)


def test_valid_price_tick_passes_and_normalizes_payload_values():
    result = validate_order_against_metadata(_intent(), _metadata())

    assert result.ok is True
    assert result.normalized_price == "100.05"
    assert result.normalized_quantity == "0.100"


def test_invalid_price_tick_fails_closed():
    result = validate_order_against_metadata(_intent(price="100.055"), _metadata())

    assert result.ok is False
    assert result.reason == "invalid_tick"


def test_quantity_below_minimum_fails():
    result = validate_order_against_metadata(_intent(quantity="0.0005"), _metadata())

    assert result.ok is False
    assert result.reason == "below_min_qty"


def test_quantity_step_mismatch_fails():
    result = validate_order_against_metadata(_intent(quantity="0.0105"), _metadata())

    assert result.ok is False
    assert result.reason == "invalid_step"


def test_market_order_uses_market_lot_size_filters():
    result = validate_order_against_metadata(
        _intent(order_type="market", price="1000.00", quantity="0.005"),
        _metadata(
            market_quantity_step="0.01",
            market_min_qty="0.01",
        ),
    )

    assert result.ok is False
    assert result.reason == "below_min_qty"


def test_notional_below_minimum_fails():
    result = validate_order_against_metadata(_intent(price="100.00", quantity="0.001"), _metadata())

    assert result.ok is False
    assert result.reason == "below_min_notional"


def test_non_trading_symbol_fails():
    result = validate_order_against_metadata(_intent(), _metadata(status="BREAK"))

    assert result.ok is False
    assert result.reason == "symbol_not_trading"


def test_unsupported_order_type_fails():
    result = validate_order_against_metadata(_intent(order_type="iceberg"), _metadata())

    assert result.ok is False
    assert result.reason == "unsupported_order_type"


def test_invalid_order_side_fails_closed():
    result = validate_order_against_metadata(
        _intent(side="hold"),
        _metadata(),
    )

    assert result.ok is False
    assert result.reason == "invalid_side"


def test_missing_metadata_fails_closed():
    result = validate_order_against_metadata(_intent(), None)

    assert result.ok is False
    assert result.reason == "missing_metadata"


def test_market_order_without_reference_price_fails_closed():
    result = validate_order_against_metadata(
        _intent(order_type="market", price=None, target_price=None),
        _metadata(),
    )

    assert result.ok is False
    assert result.reason == "missing_reference_price"


def test_order_above_maximum_notional_fails():
    result = validate_order_against_metadata(
        _intent(price="100.00", quantity="2.000"),
        _metadata(max_notional="100"),
    )

    assert result.ok is False
    assert result.reason == "above_max_notional"


def test_protective_prices_must_match_the_venue_tick():
    result = validate_order_against_metadata(
        _intent(stop_price="95.005", target_price="110.00"),
        _metadata(),
    )

    assert result.ok is False
    assert result.reason == "invalid_stop_tick"


def test_protective_prices_are_normalized_with_the_entry_bundle():
    result = validate_order_against_metadata(
        _intent(stop_price="95.00", target_price="110.00"),
        _metadata(),
    )

    assert result.ok is True
    assert result.normalized_stop_price == "95.00"
    assert result.normalized_target_price == "110.00"


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-Infinity", "0", "-1"])
def test_non_finite_or_non_positive_quantity_fails_closed(quantity):
    result = validate_order_against_metadata(_intent(quantity=quantity), _metadata())

    assert result.ok is False
    assert result.reason == "invalid_quantity"


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-Infinity", "0", "-1"])
def test_non_finite_or_non_positive_price_fails_closed(price):
    result = validate_order_against_metadata(_intent(price=price), _metadata())

    assert result.ok is False
    assert result.reason == "invalid_price"


def test_non_finite_metadata_fails_closed():
    result = validate_order_against_metadata(
        _intent(),
        _metadata(price_tick="NaN"),
    )

    assert result.ok is False
    assert result.reason == "invalid_metadata"


@pytest.mark.parametrize(
    ("side", "stop_price", "target_price", "reason"),
    [
        ("buy", "101.00", "110.00", "invalid_stop_side"),
        ("buy", "95.00", "99.00", "invalid_target_side"),
        ("sell", "99.00", "90.00", "invalid_stop_side"),
        ("sell", "105.00", "101.00", "invalid_target_side"),
    ],
)
def test_protective_prices_must_be_on_the_correct_side(side, stop_price, target_price, reason):
    result = validate_order_against_metadata(
        _intent(
            side=side,
            price="100.00",
            stop_price=stop_price,
            target_price=target_price,
        ),
        _metadata(),
    )

    assert result.ok is False
    assert result.reason == reason
