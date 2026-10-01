"""Public chart series using the graph's bounded-history indicator semantics.

Derived lines are not recorded decisions. Callers must supply the original graph,
full retained primary feed (including warmup), history window and software identity.
No graph state is evaluated here.
"""

from __future__ import annotations

import numpy as np

from koval.engine.history_window import DEFAULT_HISTORY_BARS, resolve_history_bars
from koval.strategy.graph.compat import compile_legacy, is_legacy_graph
from koval.strategy.graph.registry import get_node
from koval.strategy.helpers._math import _macd
from koval.strategy.helpers.rolling_indicators import rolling_atr, rolling_ema, rolling_rsi


def strategy_indicator_series(graph, candles, *, timeframe, history_bars=DEFAULT_HISTORY_BARS):
    window = resolve_history_bars(history_bars)
    typed = compile_legacy(graph) if is_legacy_graph(graph) else graph
    rows = np.asarray(candles, dtype=float)
    if not len(rows):
        return []
    closes = np.ascontiguousarray(rows[:, 4])
    tables, result = {}, []
    for block in typed["blocks"]:
        kind = block["type"]
        params = get_node(kind).params_schema(**(block.get("params") or {})).model_dump()
        descriptors = []
        if kind == "fact.ema_cross":
            descriptors = [("fast", "ema", params["fast"]), ("slow", "ema", params["slow"])]
        elif kind in {"policy.ema_trend", "state.trend_bias"}:
            descriptors = [("ema", "ema", params["period"])]
        elif kind in {"fact.rsi_cross", "policy.rsi"}:
            descriptors = [("current", "rsi", params["period"])]
        elif kind in {"policy.atr_volatility", "state.volatility_regime"}:
            descriptors = [("atr", "atr", params["period"])]
        elif kind == "policy.macd":
            descriptors = [(part, "macd", None) for part in ("macd", "signal", "histogram")]
        for component, indicator, period in descriptors:
            key = (
                (indicator, period)
                if indicator != "macd"
                else (indicator, params["fast"], params["slow"], params["signal_period"])
            )
            if key not in tables:
                if indicator == "ema":
                    tables[key] = rolling_ema(closes, period, window)[:, 1]
                elif indicator == "rsi":
                    tables[key] = rolling_rsi(closes, period, window)[:, 1]
                elif indicator == "atr":
                    tables[key] = rolling_atr(rows[:, 2], rows[:, 3], closes, period, window)
                else:
                    tables[key] = np.array(
                        [
                            _macd(closes[max(0, i + 1 - window) : i + 1], *key[1:])
                            if min(i + 1, window) >= params["slow"] + params["signal_period"]
                            else (np.nan,) * 3
                            for i in range(len(rows))
                        ]
                    )
            values = tables[key]
            if indicator == "macd":
                values = values[:, ("macd", "signal", "histogram").index(component)]
            minimum = (
                (
                    params["slow"] + 1
                    if kind == "fact.ema_cross"
                    else period + 2
                    if kind == "fact.rsi_cross"
                    else period + 1
                    if indicator in {"rsi", "atr"}
                    else period
                )
                if indicator != "macd"
                else params["slow"] + params["signal_period"]
            )
            points = [
                {
                    "t": int(row[0]),
                    "value": float(value)
                    if min(i + 1, window) >= minimum and np.isfinite(value)
                    else None,
                }
                for i, (row, value) in enumerate(zip(rows, values, strict=True))
            ]
            result.append(
                {
                    "node_id": block["id"],
                    "component": component,
                    "name": f"{indicator.upper()}({period})"
                    if period is not None
                    else f"MACD({params['fast']}/{params['slow']}/{params['signal_period']}) {component}",
                    "params": params,
                    "timeframe": timeframe,
                    "pane": "price" if indicator == "ema" else indicator,
                    "unit": "percent" if indicator == "rsi" else "quote",
                    "provenance": "derived",
                    "points": points,
                }
            )
    return result
