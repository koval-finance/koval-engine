"""Independent cash and inventory arithmetic for real execution deltas."""

from decimal import Decimal

import pytest

from koval.engine.spot_contract import SpotFee, SpotFill, SpotIdentity

IDENTITY = SpotIdentity("account", "BTCUSDT", "BTC", "USDT")


def fill(event="a", *, quantity="2", price="100", role="entry", fees=(), status="filled"):
    return SpotFill(
        "session",
        "entry" if role == "entry" else "target",
        "venue",
        "BTCUSDT",
        "buy" if role == "entry" else "sell",
        status,
        role,
        quantity,
        price,
        1000,
        event_id=event,
        fees=fees,
    )


def ledger():
    from koval.engine.spot_account import SpotLedger

    return SpotLedger(IDENTITY, "1000")


def test_quote_fees_cost_equity_and_partial_exit():
    account = ledger()
    account.apply(fill(fees=(SpotFee("USDT", "0.2"),)))
    snapshot = account.snapshot("110")
    assert Decimal(snapshot["quote_cash"]) == Decimal("799.8")
    assert Decimal(snapshot["inventory_cost"]) == 200
    assert Decimal(snapshot["equity"]) == Decimal("1019.8")
    account.apply(
        fill(
            "b",
            role="target",
            quantity="0.5",
            price="110",
            fees=(SpotFee("USDT", "0.055"),),
            status="partial",
        )
    )
    snapshot = account.snapshot("110")
    assert Decimal(snapshot["quote_cash"]) == Decimal("854.745")
    assert Decimal(snapshot["base_quantity"]) == Decimal("1.5")
    assert Decimal(snapshot["inventory_cost"]) == 150
    assert Decimal(snapshot["gross_realized_pnl"]) == 5
    assert Decimal(snapshot["valued_fees"]) == Decimal("0.255")
    assert Decimal(snapshot["net_realized_pnl"]) == Decimal("4.745")
    account.apply(fill("c", role="target", quantity="1.5", price="90"))
    snapshot = account.snapshot("90")
    assert Decimal(snapshot["quote_cash"]) == Decimal("989.745")
    assert Decimal(snapshot["inventory_cost"]) == 0
    assert Decimal(snapshot["equity"]) == Decimal("989.745")
    assert Decimal(snapshot["net_realized_pnl"]) == Decimal("-10.255")


def test_base_fees_reduce_inventory_without_debiting_quote_twice():
    account = ledger()
    account.apply(fill(fees=(SpotFee("BTC", "0.01"),)))
    snapshot = account.snapshot("110")
    assert Decimal(snapshot["quote_cash"]) == 800
    assert Decimal(snapshot["base_quantity"]) == Decimal("1.99")
    assert Decimal(snapshot["inventory_cost"]) == 199
    assert Decimal(snapshot["equity"]) == Decimal("1018.9")
    account.apply(
        fill("b", role="target", quantity="1.98", price="110", fees=(SpotFee("BTC", "0.01"),))
    )
    snapshot = account.snapshot("110")
    assert Decimal(snapshot["base_quantity"]) == 0
    assert Decimal(snapshot["quote_cash"]) == Decimal("1017.8")
    assert Decimal(snapshot["gross_realized_pnl"]) == Decimal("19.9")
    assert Decimal(snapshot["valued_fees"]) == Decimal("2.1")
    assert Decimal(snapshot["net_realized_pnl"]) == Decimal("17.8")


def test_other_asset_fees_are_retained_and_net_valuation_is_unavailable():
    account = ledger()
    account.apply(fill(fees=(SpotFee("BNB", "0.003"),)))
    snapshot = account.snapshot("100")
    assert snapshot["fees_by_asset"] == {"BNB": "0.003"}
    assert snapshot["valuation_complete"] is False
    assert snapshot["net_realized_pnl"] is None
    assert Decimal(snapshot["quote_cash"]) == 800
    assert snapshot["unvalued_fee_assets"] == ["BNB"]


def test_multiple_entry_deltas_have_weighted_cost_and_no_double_accounting():
    account = ledger()
    first = fill(quantity="1", status="partial")
    account.apply(first)
    assert account.apply(first) is False
    account.apply(fill("b", quantity="2", price="110"))
    assert Decimal(account.snapshot("120")["inventory_cost"]) == 320
    account.apply(fill("c", quantity="1.5", price="120", role="target", status="partial"))
    assert Decimal(account.snapshot("120")["gross_realized_pnl"]) == 20
    assert Decimal(account.snapshot("120")["inventory_cost"]) == 160


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": "NaN"},
        {"quantity": "-1"},
        {"price": "Infinity"},
        {"fees": (SpotFee("BTC", "3"),)},
        {"fees": (SpotFee("USDT", "-1"),)},
        {"quantity": "20"},
    ],
)
def test_invalid_economics_are_atomic(changes):
    account = ledger()
    before = account.snapshot("100")
    with pytest.raises(ValueError):
        account.apply(fill(**changes))
    assert account.snapshot("100") == before


def test_conflicting_duplicate_and_oversell_are_rejected():
    account = ledger()
    account.apply(fill())
    with pytest.raises(ValueError, match="conflicting"):
        account.apply(fill(price="101"))
    with pytest.raises(ValueError, match="inventory"):
        account.apply(fill("b", role="target", quantity="3"))


def test_legacy_commission_metadata_cannot_silently_lose_spot_fees():
    from dataclasses import replace

    account = ledger()
    with pytest.raises(ValueError, match="SpotFee"):
        account.apply(replace(fill(), metadata={"commission": "1", "commission_asset": "BNB"}))


def test_decimal_accounting_is_independent_of_host_decimal_context():
    from decimal import localcontext

    account = ledger()
    with localcontext() as context:
        context.prec = 6
        account.apply(fill(quantity="0.12345678", price="123.456789"))
        snapshot = account.snapshot("123.456789")
    assert Decimal(snapshot["inventory_cost"]) == Decimal("15.24157763907942")
    assert Decimal(snapshot["quote_cash"]) == Decimal("984.75842236092058")
