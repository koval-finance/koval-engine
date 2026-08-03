from koval.strategy.base.trade_setup import ChartAnnotation, StructureState, TradeSetup


def test_trade_setup_direction_long():
    setup = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    assert setup.direction == "long"


def test_trade_setup_entry_price():
    setup = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    assert setup.entry_price == 100.0


def test_trade_setup_stop_loss():
    setup = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    assert setup.stop_loss == 95.0


def test_trade_setup_default_take_profit_none():
    setup = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    assert setup.take_profit is None


def test_trade_setup_default_size_none():
    setup = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    assert setup.size is None


def test_trade_setup_default_entry_type_limit():
    setup = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    assert setup.entry_type == "limit"


def test_trade_setup_default_why_entry_empty():
    setup = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    assert setup.why_entry == []


def test_trade_setup_default_indicators_at_entry_empty():
    setup = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    assert setup.indicators_at_entry == {}


def test_trade_setup_default_annotations_empty():
    setup = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    assert setup.annotations == []


def test_trade_setup_short_direction():
    setup = TradeSetup(
        direction="short",
        entry_price=50000.0,
        stop_loss=51000.0,
        take_profit=47000.0,
        size=0.01,
        entry_type="market",
        why_entry=["BOS confirmed", "RSI overbought"],
        indicators_at_entry={"rsi": 72.3, "ema_bias": "bearish"},
        sl_calc_expr="swing_high + atr * 0.5",
        tp_calc_expr="entry - risk * 2",
    )
    assert setup.direction == "short"


def test_trade_setup_optional_take_profit():
    setup = TradeSetup(
        direction="short",
        entry_price=50000.0,
        stop_loss=51000.0,
        take_profit=47000.0,
        size=0.01,
        entry_type="market",
        why_entry=["BOS confirmed", "RSI overbought"],
        indicators_at_entry={"rsi": 72.3, "ema_bias": "bearish"},
        sl_calc_expr="swing_high + atr * 0.5",
        tp_calc_expr="entry - risk * 2",
    )
    assert setup.take_profit == 47000.0


def test_trade_setup_optional_size():
    setup = TradeSetup(
        direction="short",
        entry_price=50000.0,
        stop_loss=51000.0,
        take_profit=47000.0,
        size=0.01,
        entry_type="market",
        why_entry=["BOS confirmed", "RSI overbought"],
        indicators_at_entry={"rsi": 72.3, "ema_bias": "bearish"},
        sl_calc_expr="swing_high + atr * 0.5",
        tp_calc_expr="entry - risk * 2",
    )
    assert setup.size == 0.01


def test_trade_setup_market_entry_type():
    setup = TradeSetup(
        direction="short",
        entry_price=50000.0,
        stop_loss=51000.0,
        take_profit=47000.0,
        size=0.01,
        entry_type="market",
        why_entry=["BOS confirmed", "RSI overbought"],
        indicators_at_entry={"rsi": 72.3, "ema_bias": "bearish"},
        sl_calc_expr="swing_high + atr * 0.5",
        tp_calc_expr="entry - risk * 2",
    )
    assert setup.entry_type == "market"


def test_trade_setup_why_entry_populated():
    setup = TradeSetup(
        direction="short",
        entry_price=50000.0,
        stop_loss=51000.0,
        take_profit=47000.0,
        size=0.01,
        entry_type="market",
        why_entry=["BOS confirmed", "RSI overbought"],
        indicators_at_entry={"rsi": 72.3, "ema_bias": "bearish"},
        sl_calc_expr="swing_high + atr * 0.5",
        tp_calc_expr="entry - risk * 2",
    )
    assert len(setup.why_entry) == 2


def test_trade_setup_indicators_at_entry_populated():
    setup = TradeSetup(
        direction="short",
        entry_price=50000.0,
        stop_loss=51000.0,
        take_profit=47000.0,
        size=0.01,
        entry_type="market",
        why_entry=["BOS confirmed", "RSI overbought"],
        indicators_at_entry={"rsi": 72.3, "ema_bias": "bearish"},
        sl_calc_expr="swing_high + atr * 0.5",
        tp_calc_expr="entry - risk * 2",
    )
    assert setup.indicators_at_entry["rsi"] == 72.3


def test_structure_state_defaults():
    state = StructureState()
    assert state.bias == "neutral"
    assert state.bos_confirmed is False
    assert state.choch_detected is False
    assert state.ltf_choch_detected is False
    assert state.last_swing_high is None
    assert state.last_swing_low is None
    assert state.active_poi is None
    assert state.poi_zone_upper is None
    assert state.poi_zone_lower is None


def test_structure_state_with_values():
    state = StructureState(
        bias="bullish",
        last_swing_high=100.0,
        last_swing_low=90.0,
        bos_confirmed=True,
        active_poi=92.5,
    )
    assert state.bias == "bullish"
    assert state.bos_confirmed is True
    assert state.active_poi == 92.5


def test_chart_annotation_level():
    ann = ChartAnnotation(type="level", time=1700000000, value=100.0)
    assert ann.type == "level"
    assert ann.color == "#ffffff"
    assert ann.label == ""
    assert ann.zone_top is None
    assert ann.zone_bottom is None


def test_chart_annotation_zone():
    ann = ChartAnnotation(
        type="zone",
        time=1700000000,
        value=99.0,
        zone_top=101.0,
        zone_bottom=97.0,
        color="#ff0000",
        label="FVG",
    )
    assert ann.zone_top == 101.0
    assert ann.zone_bottom == 97.0
    assert ann.label == "FVG"


def test_trade_setup_why_entry_list_is_independent():
    a = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    b = TradeSetup(direction="long", entry_price=100.0, stop_loss=95.0)
    a.why_entry.append("signal")
    assert b.why_entry == []
