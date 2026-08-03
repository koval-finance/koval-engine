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
