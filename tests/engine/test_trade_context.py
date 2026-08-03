from koval.engine.trade_context import context_from_raw


def test_context_from_raw_maps_rich_keys():
    raw = {
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "sl_calculation": "entry - 1xATR",
        "tp_calculation": "entry + 3xATR",
        "exit_reason": "Take Profit",
        "why_entry": ["EMA cross up", "trend filter passed"],
        "indicators_at_entry": {"RSI": 41.0},
        "sl_history": [{"ts": "t0", "price": 95.0, "reason": "initial"}],
    }

    ctx = context_from_raw(raw, trade_id=0)

    assert ctx["trade_id"] == 0
    assert ctx["stop_loss"] == 95.0
    assert ctx["take_profit"] == 110.0
    assert ctx["sl_calculation"] == "entry - 1xATR"
    assert ctx["exit_reason"] == "tp"
    assert ctx["exit_reason_text"] == "Take Profit"
    assert ctx["pattern"] == "EMA cross up"
    assert ctx["why_entry"] == ["EMA cross up", "trend filter passed"]


def test_context_from_raw_defaults_for_missing_keys():
    ctx = context_from_raw({}, trade_id=3)

    assert ctx["trade_id"] == 3
    assert ctx["exit_reason"] == "other"
    assert ctx["why_entry"] == []
    assert ctx["sl_history"] == []
    assert ctx["pattern"] == ""
