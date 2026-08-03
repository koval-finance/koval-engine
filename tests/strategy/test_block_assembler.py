"""Tests for `koval.strategy.block_assembler.assemble_from_graph`.

``assemble_from_graph`` is a dispatcher onto the typed dataflow engine:

* complete, connected legacy linear-AND graphs are compiled to a native typed
  graph by the compatibility compiler, then run through the typed engine;
* native typed graphs (typed node types, ``from_port``/``to_port`` edges) are
  validated and built directly.

What ``assemble_from_graph`` still rejects (via the typed ``validate_graph``):
unknown node types, invalid block params, duplicate ids, and — for native
graphs — port/domain/type/cycle violations (those are covered in
``tests/strategy/graph/test_validation.py``).

Legacy validation is stricter than permissive parsing: graphs require at least
one connected signal plus exactly one entry, static exit, and risk block.
Malformed or disconnected graphs fail rather than acquiring hidden defaults.
Behavioural surface (intent-gated entries, dynamic exits) is covered in
``tests/strategy/graph/test_graph_strategy.py`` and ``test_dynamic_exit.py``.
"""

from __future__ import annotations

import numpy as np
import pytest

from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.block_assembler import GraphValidationError, assemble_from_graph


def _minimal_graph(**overrides) -> dict:
    g = {
        "blocks": [
            {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 9, "slow": 21}},
            {"id": "ent", "type": "entry.both", "params": {"entry_type": "market"}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {"sl_pct": 2.0, "risk_reward": 2.0}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1.0}},
        ],
        "connections": [
            {"from": "sig", "to": "ent"},
            {"from": "ent", "to": "ex"},
            {"from": "ex", "to": "rsk"},
        ],
    }
    g.update(overrides)
    return g


def _inject(s, closes):
    s.close = float(closes[-1])
    s.high = s.close
    s.low = s.close
    s.open = s.close
    s.volume = 1.0
    s.bar_index = len(closes) - 1
    s.timestamp_ms = s.bar_index * 1000
    s.closes = np.array(closes, float)
    s.highs = s.closes.copy()
    s.lows = s.closes.copy()
    s.opens = s.closes.copy()
    s.volumes = np.ones_like(s.closes)
    s.account_value = 10_000.0


# ---------------------------------------------------------------------------
# Happy path — legacy + native both assemble
# ---------------------------------------------------------------------------


def test_minimal_legacy_graph_assembles_to_declarative_strategy():
    s = assemble_from_graph(_minimal_graph())
    assert isinstance(s, DeclarativeStrategy)


def test_native_typed_graph_assembles_to_declarative_strategy():
    native = {
        "blocks": [
            {"id": "sig", "type": "fact.ema_cross", "params": {"fast": 9, "slow": 21}},
            {"id": "conf", "type": "interp.confluence_and", "params": {}},
            {"id": "gate", "type": "interp.direction_gate", "params": {}},
            {"id": "ord", "type": "exec.order_constructor", "params": {}},
        ],
        "connections": [
            {"from": "sig", "from_port": "event", "to": "conf", "to_port": "events"},
            {"from": "conf", "from_port": "agreement", "to": "gate", "to_port": "agreement"},
            {"from": "gate", "from_port": "intent", "to": "ord", "to_port": "intent"},
        ],
    }
    s = assemble_from_graph(native)
    assert isinstance(s, DeclarativeStrategy)


# ---------------------------------------------------------------------------
# Validation still enforced — block types & params
# ---------------------------------------------------------------------------


def test_unknown_block_type_raises():
    # Unknown type -> not a legacy graph -> typed validation rejects it.
    g = _minimal_graph(
        blocks=[
            {"id": "x", "type": "signal.does_not_exist", "params": {}},
            {"id": "ent", "type": "entry.both", "params": {}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {}},
        ],
        connections=[],
    )
    with pytest.raises(GraphValidationError):
        assemble_from_graph(g)


def test_invalid_params_raises():
    # fast >= slow violates EmaCrossParams.model_validator
    g = _minimal_graph(
        blocks=[
            {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 99, "slow": 9}},
            {"id": "ent", "type": "entry.both", "params": {}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {}},
        ]
    )
    with pytest.raises(GraphValidationError):
        assemble_from_graph(g)


def test_unknown_param_key_rejected_by_strict_schema():
    g = _minimal_graph(
        blocks=[
            {
                "id": "sig",
                "type": "signal.ema_cross",
                "params": {"fast": 9, "slow": 21, "ghost": 1},
            },
            {"id": "ent", "type": "entry.both", "params": {}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {}},
        ]
    )
    with pytest.raises(GraphValidationError):
        assemble_from_graph(g)


def test_duplicate_ids_raises():
    # Two blocks share id "sig"; the compiled typed graph carries the dup,
    # which the typed validator rejects.
    g = _minimal_graph(
        blocks=[
            {"id": "sig", "type": "signal.ema_cross", "params": {}},
            {"id": "sig", "type": "filter.adx", "params": {}},
            {"id": "ent", "type": "entry.both", "params": {}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {}},
        ]
    )
    with pytest.raises(GraphValidationError):
        assemble_from_graph(g)


# ---------------------------------------------------------------------------
# Legacy structure and topology
# ---------------------------------------------------------------------------


def test_missing_entry_exit_risk_rejected_without_hidden_defaults():
    g = {
        "blocks": [{"id": "sig", "type": "signal.ema_cross", "params": {}}],
        "connections": [],
    }
    with pytest.raises(GraphValidationError, match="entry"):
        assemble_from_graph(g)


def test_multiple_entries_rejected_instead_of_using_first():
    g = _minimal_graph(
        blocks=[
            {"id": "sig", "type": "signal.ema_cross", "params": {}},
            {"id": "ent1", "type": "entry.both", "params": {}},
            {"id": "ent2", "type": "entry.long_only", "params": {}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {}},
        ],
        connections=[
            {"from": "sig", "to": "ent1"},
            {"from": "ent1", "to": "ex"},
            {"from": "ex", "to": "rsk"},
        ],
    )
    with pytest.raises(GraphValidationError, match="exactly one entry"):
        assemble_from_graph(g)


def test_legacy_dangling_connection_rejected():
    g = _minimal_graph(connections=[{"from": "sig", "to": "ghost"}])
    with pytest.raises(GraphValidationError, match="unknown id"):
        assemble_from_graph(g)


def test_disconnected_signal_rejected_instead_of_silently_joined():
    g = _minimal_graph(
        blocks=[
            {"id": "sig", "type": "signal.ema_cross", "params": {}},
            {"id": "unused", "type": "signal.bos", "params": {}},
            {"id": "ent", "type": "entry.both", "params": {}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {}},
        ]
    )
    with pytest.raises(GraphValidationError, match="unused.*entry"):
        assemble_from_graph(g)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"blocks": {}, "connections": []},
        {"blocks": [None], "connections": []},
        {"blocks": [], "connections": {}},
    ],
)
def test_malformed_payload_normalized_to_graph_validation_error(payload):
    with pytest.raises(GraphValidationError):
        assemble_from_graph(payload)


def test_invalid_dynamic_exit_params_normalized_to_graph_validation_error():
    g = _minimal_graph(
        blocks=[
            {"id": "sig", "type": "signal.ema_cross", "params": {}},
            {"id": "ent", "type": "entry.both", "params": {}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "be", "type": "exit.breakeven", "params": {"trigger_r": 0}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {}},
        ],
        connections=[
            {"from": "sig", "to": "ent"},
            {"from": "ent", "to": "ex"},
            {"from": "ex", "to": "be"},
            {"from": "be", "to": "rsk"},
        ],
    )
    with pytest.raises(GraphValidationError, match="params invalid"):
        assemble_from_graph(g)


# ---------------------------------------------------------------------------
# Behavioural surface — intent-gated entries
# ---------------------------------------------------------------------------


def test_should_long_false_without_signal_data():
    s = assemble_from_graph(_minimal_graph())
    s.closes = None
    assert s.should_long() is False


def test_should_long_true_and_go_long_builds_bracket_on_golden_cross():
    g = _minimal_graph(
        blocks=[
            {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 2, "slow": 3}},
            {"id": "ent", "type": "entry.long_only", "params": {"entry_type": "market"}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {"sl_pct": 2.0, "risk_reward": 2.0}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1.0}},
        ],
    )
    s = assemble_from_graph(g)
    _inject(s, [10, 9, 8, 7, 12])  # fast crosses above slow -> long, entry=12
    assert s.should_long() is True
    setup = s.go_long()
    assert setup.direction == "long"
    assert setup.entry_price == pytest.approx(12.0)
    assert setup.stop_loss == pytest.approx(12.0 * 0.98)  # sl_pct=2
    assert setup.take_profit == pytest.approx(12.0 + 2 * (12.0 - 12.0 * 0.98))  # rr=2
    assert setup.size > 0
    assert setup.entry_type == "market"


def test_multi_signal_and_blocks_when_one_signal_does_not_fire():
    # Legacy linear-AND parity: every wired signal must fire and agree. Here
    # ema_cross fires bullish but bos(lookback=10) cannot (too few bars), so the
    # AND must block the entry — not fire on partial confirmation.
    g = {
        "blocks": [
            {"id": "s1", "type": "signal.ema_cross", "params": {"fast": 2, "slow": 3}},
            {"id": "s2", "type": "signal.bos", "params": {"lookback": 10}},
            {"id": "ent", "type": "entry.long_only", "params": {}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {}},
        ],
        "connections": [
            {"from": "s1", "to": "ent"},
            {"from": "s2", "to": "ent"},
            {"from": "ent", "to": "ex"},
            {"from": "ex", "to": "rsk"},
        ],
    }
    s = assemble_from_graph(g)
    _inject(s, [10, 9, 8, 7, 12])
    assert s.should_long() is False


def test_entry_type_preserved_through_compat():
    # The entry block's order type must survive compat -> order_constructor ->
    # TradeSetup (it used to be silently downgraded to "market").
    g = _minimal_graph(
        blocks=[
            {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 2, "slow": 3}},
            {"id": "ent", "type": "entry.long_only", "params": {"entry_type": "limit"}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {}},
        ],
    )
    s = assemble_from_graph(g)
    _inject(s, [10, 9, 8, 7, 12])
    assert s.go_long().entry_type == "limit"


def test_long_only_never_emits_short_intent():
    g = _minimal_graph(
        blocks=[
            {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 2, "slow": 3}},
            {"id": "ent", "type": "entry.long_only", "params": {}},
            {"id": "ex", "type": "exit.fixed_sl_tp", "params": {}},
            {"id": "rsk", "type": "risk.pct_risk", "params": {}},
        ],
    )
    s = assemble_from_graph(g)
    _inject(s, [10, 11, 12, 13, 5])  # fast crosses below slow -> bearish
    assert s.should_short() is False
