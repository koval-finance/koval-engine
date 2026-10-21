"""Known opening prices precede ambiguous intrabar protective touches."""

import pytest

from koval.engine.paper_broker import PaperBroker
from koval.engine.paper_profile import (
    PAPER_FIXED_VERSION,
    PAPER_LEGACY_VERSION,
    PAPER_REALISTIC_VERSION,
    resolve_paper_profile,
)

VERSIONS = [PAPER_LEGACY_VERSION, PAPER_FIXED_VERSION, PAPER_REALISTIC_VERSION]


def _broker(version):
    config = {"version": version}
    if version != PAPER_LEGACY_VERSION:
        config.update(commission_bps=4.0, spread_bps=20.0, slippage_bps=10.0)
    return PaperBroker(10_000, profile=resolve_paper_profile(config))


def _submit(broker, side, *, order_type="market"):
    broker.submit_bracket(
        side=side,
        entry_price=100,
        stop_price=90 if side == "buy" else 110,
        target_price=120 if side == "buy" else 80,
        quantity=1,
        order_type=order_type,
    )


def _open_position(broker, side):
    _submit(broker, side)
    if broker.profile.is_costed:
        broker.process_bar(ts_ms=60_000, open=100, high=101, low=99, close=100)
    else:
        broker.fill_market_if_pending(ts_ms=60_000, price=100)


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize(
    ("side", "opening"), [("buy", 120.0), ("buy", 125.0), ("sell", 80.0), ("sell", 75.0)]
)
def test_working_target_at_open_precedes_later_stop_touch(version, side, opening):
    broker = _broker(version)
    _open_position(broker, side)

    (fill,) = broker.process_bar(ts_ms=120_000, open=opening, high=130, low=70, close=100)

    assert fill.kind == "take_profit"
    assert fill.reference_price == opening
    adjustment = 0.998 if side == "buy" else 1.002
    expected_price = opening * adjustment if broker.profile.is_costed else opening
    assert fill.price == pytest.approx(expected_price)
    assert broker.position is None
    assert broker.resolved_metadata["ambiguities"] == []


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_open_inside_bracket_keeps_conservative_intrabar_stop_priority(version, side):
    broker = _broker(version)
    _open_position(broker, side)

    (fill,) = broker.process_bar(ts_ms=120_000, open=100, high=130, low=70, close=100)

    assert fill.kind == "stop_loss"
    assert fill.reference_price == (90 if side == "buy" else 110)


@pytest.mark.parametrize("version", [PAPER_LEGACY_VERSION, PAPER_REALISTIC_VERSION])
@pytest.mark.parametrize(
    ("side", "opening", "high", "low", "close", "target"),
    [("buy", 125, 126, 95, 110, 120), ("sell", 75, 105, 74, 90, 80)],
)
def test_intrabar_limit_entry_cannot_exit_at_the_earlier_bar_open(
    version, side, opening, high, low, close, target
):
    broker = _broker(version)
    _submit(broker, side, order_type="limit")

    entry, exit_fill = broker.process_bar(
        ts_ms=60_000, open=opening, high=high, low=low, close=close
    )

    assert entry.kind == "entry"
    assert entry.reference_price == 100
    assert exit_fill.kind == "take_profit"
    assert exit_fill.reference_price == target
