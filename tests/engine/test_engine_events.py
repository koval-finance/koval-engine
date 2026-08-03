from koval.engine.engine_events import EngineEvent, EventType


def test_event_type_has_required_values():
    required = {
        "SESSION_START",
        "SESSION_END",
        "SIGNAL_DETECTED",
        "FILTER_PASSED",
        "FILTER_REJECTED",
        "ORDER_PLACED",
        "ORDER_FILLED",
        "TRADE_OPENED",
        "TRADE_CLOSED",
        "DRAWDOWN_LIMIT_HIT",
    }
    actual = {e.value for e in EventType}
    assert required <= actual, f"Missing EventTypes: {required - actual}"


def test_engine_event_stores_fields():
    evt = EngineEvent(
        event_type=EventType.SIGNAL_DETECTED,
        bar_index=42,
        timestamp_ms=1700000000000,
        payload={"direction": "long", "why": ["BOS confirmed"]},
    )
    assert evt.event_type == EventType.SIGNAL_DETECTED
    assert evt.bar_index == 42
    assert evt.timestamp_ms == 1700000000000
    assert evt.payload["direction"] == "long"


def test_engine_event_default_payload_is_empty():
    evt = EngineEvent(
        event_type=EventType.SESSION_START,
        bar_index=0,
        timestamp_ms=0,
    )
    assert evt.payload == {}


def test_engine_event_payload_independent_across_instances():
    a = EngineEvent(EventType.TRADE_OPENED, bar_index=1, timestamp_ms=0)
    b = EngineEvent(EventType.TRADE_OPENED, bar_index=2, timestamp_ms=0)
    a.payload["x"] = 1
    assert "x" not in b.payload


def test_no_backtrader_import_in_engine_events():
    import importlib
    import sys

    mod_name = "koval.engine.engine_events"
    mod = sys.modules.get(mod_name) or importlib.import_module(mod_name)
    for name in dir(mod):
        if name.startswith("_"):
            continue
        obj = getattr(mod, name)
        module_of = getattr(obj, "__module__", "") or ""
        assert not module_of.startswith("backtrader"), (
            f"backtrader symbol '{name}' in engine_events — keep BT in adapters/backtrader/"
        )
