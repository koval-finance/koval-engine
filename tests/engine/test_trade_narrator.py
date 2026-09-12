import koval.engine.trade_narrator as trade_narrator


def test_module_exposes_only_the_framework_free_narrative_builder():
    assert not hasattr(trade_narrator, "TradeNarrator")


def test_build_narrative_from_context_and_trade():
    from koval.engine.trade_narrator import build_narrative

    context = {
        "exit_reason": "tp",
        "exit_reason_text": "Take Profit",
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "pattern": "EMA cross up",
        "why_entry": ["EMA cross up"],
    }
    trade = {
        "direction": "long",
        "entry_price": 100.0,
        "exit_price": 110.0,
        "size": 1.0,
        "pnl": 10.0,
        "pnl_pct": 10.0,
    }

    text = build_narrative(context, trade)

    assert "Long" in text
    assert "Take Profit" in text
    assert "$110" in text or "110" in text


def test_favorable_price_move_is_independent_of_net_loss_and_funding():
    text = trade_narrator.build_narrative(
        {"exit_reason_text": "Take Profit"},
        {
            "direction": "long",
            "entry_price": 100,
            "exit_price": 101,
            "pnl": -2,
            "pnl_pct": -2,
            "funding": 99,
        },
    )
    assert "Price moved 1.00% in favor" in text
    assert "$-2.00 net" in text
    assert "Funding" not in text
    assert "Entry reason not recorded" in text


def test_short_price_movement_uses_actual_prices():
    text = trade_narrator.build_narrative(
        {},
        {"direction": "short", "entry_price": 100, "exit_price": 102, "pnl": 1, "pnl_pct": 1},
    )
    assert "Price moved 2.00% against the position" in text
    assert "$+1.00 net" in text


def test_missing_values_are_not_invented_as_zero_or_short():
    text = trade_narrator.build_narrative({}, {})
    assert "not recorded" in text
    assert "$0.00" not in text
    assert "Short" not in text


def test_unchanged_price_is_not_described_as_favorable():
    text = trade_narrator.build_narrative(
        {},
        {"direction": "long", "entry_price": 100, "exit_price": 100, "pnl": -1},
    )
    assert "Price was unchanged" in text
