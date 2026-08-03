from koval.strategy.graph.entities import MarketEvent, MarketState, PolicyDecision
from koval.strategy.graph.ports import PortSpec, port_accepts


def test_input_port_accepts_listed_type():
    p = PortSpec(name="in", accepts=(MarketEvent, MarketState))
    assert port_accepts(p, MarketEvent) is True
    assert port_accepts(p, MarketState) is True


def test_input_port_rejects_unlisted_type():
    p = PortSpec(name="in", accepts=(MarketEvent,))
    assert port_accepts(p, PolicyDecision) is False


def test_port_required_default_true():
    assert PortSpec(name="in", accepts=(MarketEvent,)).required is True


def test_input_cardinality_defaults_to_one_connection():
    assert PortSpec(name="in", accepts=(MarketEvent,)).max_connections == 1


def test_fan_in_port_allows_unbounded_connections():
    port = PortSpec(name="events", accepts=(MarketEvent,), max_connections=None)
    assert port.max_connections is None


def test_output_port_is_not_terminal_by_default():
    assert PortSpec(name="out", accepts=(MarketEvent,)).terminal is False
