from koval.strategy.helpers.risk.sizing import required_margin


def test_futures_margin_is_notional_over_leverage():
    # notional = 2 * 100 = 200; leverage 4 → 50
    assert (
        required_margin(quantity=2.0, entry_price=100.0, leverage=4.0, instrument_type="futures")
        == 50.0
    )


def test_spot_margin_is_full_notional():
    # spot ignores leverage → full notional
    assert (
        required_margin(quantity=2.0, entry_price=100.0, leverage=10.0, instrument_type="spot")
        == 200.0
    )


def test_zero_leverage_guarded():
    assert (
        required_margin(quantity=1.0, entry_price=100.0, leverage=0.0, instrument_type="futures")
        == 100.0
    )
