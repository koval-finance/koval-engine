import pickle

import koval.strategy.nodes  # noqa: F401
from koval.strategy.graph.entities import MarketEvent, SetupCandidate
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.registry import get_node


def _ctx(i: int) -> BarContext:
    return BarContext(
        close=1.0, high=1.0, low=1.0, open=1.0, volume=1.0, bar_index=i, timestamp_ms=i * 1000
    )


def _ev(kind: str, direction: str, bar: int, node_id: str = "src") -> MarketEvent:
    return MarketEvent(
        bar_index=bar,
        timestamp_ms=bar * 1000,
        source_node_id=node_id,
        kind=kind,
        direction=direction,
    )


def _agg(**params):
    spec = get_node("interp.event_aggregator")
    return spec.factory(spec.params_schema(**params))


def test_strict_in_order_completes_and_emits_on_last_bar():
    ev = _agg(event_sequence=["order_block", "choch"], timeout_bars=10)
    state: dict = {}
    assert ev(_ctx(3), {"events": [_ev("order_block", "bullish", 3)]}, state)["candidate"] is None
    out = ev(_ctx(7), {"events": [_ev("choch", "bullish", 7)]}, state)
    cand = out["candidate"]
    assert isinstance(cand, SetupCandidate)
    assert cand.direction == "bullish"
    assert cand.window == (3, 7)
    assert cand.contributing_events == [("src", 3), ("src", 7)]
    assert cand.metadata["event_kinds"] == ["order_block", "choch"]
    assert cand.setup_type == "order_block->choch"


def test_partial_chain_does_not_emit():
    ev = _agg(event_sequence=["order_block", "choch"], timeout_bars=10)
    state: dict = {}
    assert ev(_ctx(3), {"events": [_ev("order_block", "bullish", 3)]}, state)["candidate"] is None


def test_timeout_drops_chain_before_completion():
    ev = _agg(event_sequence=["order_block", "choch"], timeout_bars=2)
    state: dict = {}
    ev(_ctx(0), {"events": [_ev("order_block", "bullish", 0)]}, state)
    # choch arrives after the 2-bar window -> chain already evicted, no completion
    out = ev(_ctx(5), {"events": [_ev("choch", "bullish", 5)]}, state)
    assert out["candidate"] is None


def test_any_order_completes_when_reversed():
    ev = _agg(event_sequence=["order_block", "choch"], order_policy="any", timeout_bars=10)
    state: dict = {}
    ev(_ctx(1), {"events": [_ev("choch", "bullish", 1)]}, state)
    out = ev(_ctx(4), {"events": [_ev("order_block", "bullish", 4)]}, state)
    assert out["candidate"] is not None


def test_strict_order_rejects_reversed():
    ev = _agg(event_sequence=["order_block", "choch"], order_policy="strict", timeout_bars=10)
    state: dict = {}
    ev(_ctx(1), {"events": [_ev("choch", "bullish", 1)]}, state)
    out = ev(_ctx(4), {"events": [_ev("order_block", "bullish", 4)]}, state)
    # 'choch' is not seq[0], so nothing seeded; 'order_block' seeds but never completes
    assert out["candidate"] is None


def test_direction_consistency_blocks_conflicting_under_consistent():
    ev = _agg(
        event_sequence=["order_block", "choch"], direction_policy="consistent", timeout_bars=10
    )
    state: dict = {}
    ev(_ctx(1), {"events": [_ev("order_block", "bullish", 1)]}, state)
    out = ev(_ctx(3), {"events": [_ev("choch", "bearish", 3)]}, state)
    assert out["candidate"] is None


def test_direction_ignore_completes_neutral():
    ev = _agg(event_sequence=["order_block", "choch"], direction_policy="ignore", timeout_bars=10)
    state: dict = {}
    ev(_ctx(1), {"events": [_ev("order_block", "bullish", 1)]}, state)
    out = ev(_ctx(3), {"events": [_ev("choch", "bearish", 3)]}, state)
    assert out["candidate"].direction == "neutral"


def test_max_open_chains_caps_buffer():
    ev = _agg(event_sequence=["order_block", "choch"], max_open_chains=2, timeout_bars=50)
    state: dict = {}
    for bar in range(5):
        ev(_ctx(bar), {"events": [_ev("order_block", "bullish", bar)]}, state)
    assert len(state["chains"]) == 2
    # cap keeps the most-recently-seeded partials (drops oldest)
    assert [c["matched"][0][1] for c in state["chains"]] == [3, 4]


def test_setup_ids_unique_across_emissions():
    ev = _agg(event_sequence=["order_block", "choch"], timeout_bars=50)
    state: dict = {}
    ev(_ctx(0), {"events": [_ev("order_block", "bullish", 0)]}, state)
    first = ev(_ctx(1), {"events": [_ev("choch", "bullish", 1)]}, state)["candidate"]
    ev(_ctx(2), {"events": [_ev("order_block", "bullish", 2)]}, state)
    second = ev(_ctx(3), {"events": [_ev("choch", "bullish", 3)]}, state)["candidate"]
    assert first.setup_id != second.setup_id


def test_cap_does_not_evict_a_chain_that_completes_this_bar():
    # max_open_chains=1: on bar 1 the oldest chain (seeded bar 0) completes via
    # 'choch' while a fresh 'order_block' seeds a new chain in the same bar. The
    # cap must not drop the completed chain before it is emitted.
    ev = _agg(event_sequence=["order_block", "choch"], max_open_chains=1, timeout_bars=50)
    state: dict = {}
    ev(_ctx(0), {"events": [_ev("order_block", "bullish", 0)]}, state)
    out = ev(
        _ctx(1),
        {"events": [_ev("choch", "bullish", 1), _ev("order_block", "bullish", 1)]},
        state,
    )
    assert out["candidate"] is not None
    assert out["candidate"].window[0] == 0


def test_multiple_complete_same_bar_emits_earliest():
    ev = _agg(event_sequence=["order_block", "choch"], timeout_bars=50)
    state: dict = {}
    ev(_ctx(0), {"events": [_ev("order_block", "bullish", 0)]}, state)  # chain A
    ev(_ctx(1), {"events": [_ev("order_block", "bullish", 1)]}, state)  # chain B
    out = ev(
        _ctx(2),
        {"events": [_ev("choch", "bullish", 2, "a"), _ev("choch", "bullish", 2, "b")]},
        state,
    )
    assert out["candidate"].window[0] == 0  # earliest-started chain wins


def test_node_state_is_picklable():
    ev = _agg(event_sequence=["order_block", "choch"], timeout_bars=10)
    state: dict = {}
    ev(_ctx(3), {"events": [_ev("order_block", "bullish", 3)]}, state)
    restored = pickle.loads(pickle.dumps(state))
    assert restored == state


def _cand(setup_type="order_block->choch", direction="bullish") -> SetupCandidate:
    return SetupCandidate(
        bar_index=7,
        timestamp_ms=7000,
        source_node_id="event_aggregator",
        setup_id="src@3-7",
        setup_type=setup_type,
        direction=direction,
        contributing_events=[("src", 3), ("src", 7)],
        window=(3, 7),
        metadata={"event_kinds": ["order_block", "choch"]},
    )


def _gen(**params):
    spec = get_node("interp.setup_generator")
    return spec.factory(spec.params_schema(**params))


def test_setup_generator_remaps_type():
    ev = _gen(setup_type_map={"order_block->choch": "ob_reversal"})
    out = ev(_ctx(7), {"candidate": [_cand()]}, {})
    assert out["candidate"].setup_type == "ob_reversal"
    assert out["candidate"].setup_id == "src@3-7"
    assert out["candidate"].metadata["event_kinds"] == ["order_block", "choch"]


def test_setup_generator_passes_unmapped_type_through():
    ev = _gen(setup_type_map={"other": "x"})
    out = ev(_ctx(7), {"candidate": [_cand()]}, {})
    assert out["candidate"].setup_type == "order_block->choch"


def test_setup_generator_inverts_direction():
    ev = _gen(direction_mode="invert")
    out = ev(_ctx(7), {"candidate": [_cand(direction="bearish")]}, {})
    assert out["candidate"].direction == "bullish"


def test_setup_generator_no_candidate_returns_none():
    ev = _gen()
    assert ev(_ctx(7), {"candidate": []}, {})["candidate"] is None
