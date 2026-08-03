"""Compat fidelity: legacy dynamic-exit blocks (trailing / breakeven) keep
working through the typed engine. The dynamic-exit callable is carried on the
compat GraphStrategy seam (the typed graph itself has no dynamic_exit node)."""

import numpy as np
import pytest

import koval.strategy.nodes  # noqa: F401
from koval.strategy.block_assembler import assemble_from_graph

_BASE = [
    {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 2, "slow": 3}},
    {"id": "ent", "type": "entry.long_only", "params": {"entry_type": "market"}},
    {"id": "ex", "type": "exit.fixed_sl_tp", "params": {"sl_pct": 2.0, "risk_reward": 2.0}},
    {"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1.0}},
]


def _graph(*extra_blocks):
    connections = [
        {"from": "sig", "to": "ent"},
        {"from": "ent", "to": "ex"},
    ]
    previous = "ex"
    for block in extra_blocks:
        connections.append({"from": previous, "to": block["id"]})
        previous = block["id"]
    connections.append({"from": previous, "to": "rsk"})
    return {
        "blocks": _BASE[:3] + list(extra_blocks) + _BASE[3:],
        "connections": connections,
    }


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


def test_no_dynamic_exit_returns_none_from_on_sl_update():
    s = assemble_from_graph(_graph())
    _inject(s, [10, 9, 8, 7, 12])
    setup = s.go_long()
    s.on_open_position(trade_id=1, setup=setup)
    assert s.on_sl_update(trade_id=1) is None


def test_breakeven_moves_stop_to_entry_through_typed_engine():
    s = assemble_from_graph(
        _graph({"id": "be", "type": "exit.breakeven", "params": {"trigger_r": 1.0}})
    )
    _inject(s, [10, 9, 8, 7, 12])  # golden cross -> long, entry=12
    setup = s.go_long()
    s.on_open_position(trade_id=1, setup=setup)
    # entry=12, sl=11.76 -> risk=0.24; price to entry + 1R = 12.24 triggers BE
    s.close = 12.24
    new_sl = s.on_sl_update(trade_id=1)
    assert new_sl == pytest.approx(setup.entry_price)
    # once moved, the same call must not re-emit the same stop
    assert s.on_sl_update(trade_id=1) is None


def test_close_position_resets_dynamic_exit_state():
    s = assemble_from_graph(
        _graph({"id": "be", "type": "exit.breakeven", "params": {"trigger_r": 1.0}})
    )
    _inject(s, [10, 9, 8, 7, 12])
    setup = s.go_long()
    s.on_open_position(trade_id=1, setup=setup)
    s.on_close_position(trade_id=1, result={})
    # after close, on_sl_update short-circuits (no open setup)
    s.close = 200.0
    assert s.on_sl_update(trade_id=1) is None
