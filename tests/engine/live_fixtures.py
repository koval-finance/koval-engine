"""Test fixtures for live engine tests.

Test fixture: an EMA-cross typed graph tuned (short trend_period, entry=both,
bearish) so a synthetic up-then-down ramp produces a deterministic short trade.
NOT the canonical strategy (that is seeds/ema_cross_trend.json).
"""

from __future__ import annotations


def ema_cross_graph() -> dict:
    """Return an EMA-cross-trend block graph tuned for deterministic test behaviour.

    Test fixture: an EMA-cross typed graph tuned (short trend_period, entry=both,
    bearish) so a synthetic up-then-down ramp produces a deterministic short trade.
    NOT the canonical strategy (that is seeds/ema_cross_trend.json).

    Differs from the seeded ``ema_cross_trend.json`` in three ways:

    * ``trend_period`` is the slow period (seed: 200). A 200-bar trend filter
      never warms up on unit-test windows.
    * ``entry`` allows both directions and ``trend_direction`` is bearish
      (seed: long-only, bullish). A monotonic up-then-down test ramp produces
      only a *bearish* EMA cross (the fast EMA starts above the slow EMA, so no
      golden cross ever forms), so a long-only + bullish-trend strategy cannot
      trade on it. A short on the bearish cross during the down-leg is the only
      crossover the canonical ramp offers.
    """
    return {
        "blocks": [
            {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 9, "slow": 21}},
            {
                "id": "trend",
                "type": "filter.ema_trend",
                "params": {"period": 21, "direction": "bearish"},
            },
            {"id": "ent", "type": "entry.both", "params": {"entry_type": "market"}},
            {
                "id": "ex",
                "type": "exit.fixed_sl_tp",
                "params": {"sl_pct": 2.0, "risk_reward": 2.0},
            },
            {"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1.0}},
        ],
        "connections": [
            {"from": "sig", "to": "trend"},
            {"from": "trend", "to": "ent"},
            {"from": "ent", "to": "ex"},
            {"from": "ex", "to": "rsk"},
        ],
    }
