"""Derived numeric chart points use the same historical evaluator as nodes."""

from koval.exchanges.base import timeframe_ms
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.values import ValueSpecV1, value_at


def numeric_value_series(block, rows, timeframe, symbol, window):
    if not symbol:
        raise ValueError("numeric value series requires an explicit symbol")
    step = timeframe_ms(timeframe)
    spec = ValueSpecV1(**(block.get("params") or {}))
    points = []
    for i, row in enumerate(rows):
        start = max(0, i + 1 - window)
        history = rows[start : i + 1]
        ctx = BarContext(
            close=float(row[4]),
            high=float(row[2]),
            low=float(row[3]),
            open=float(row[1]),
            volume=float(row[5]),
            bar_index=i,
            timestamp_ms=int(row[0]),
            closes=history[:, 4],
            highs=history[:, 2],
            lows=history[:, 3],
            opens=history[:, 1],
            volumes=history[:, 5],
            symbol=symbol,
            timeframe=timeframe,
            history_start_ms=int(history[0, 0]),
            decision_timestamp_ms=int(row[0]) + step,
        )
        value = value_at(ctx, spec)
        points.append(
            {
                "t": int(row[0]),
                **value.model_dump(
                    include={
                        "value",
                        "readiness",
                        "reason",
                        "as_of_ms",
                        "available_at_ms",
                        "input_cursor",
                    }
                ),
            }
        )
    return {
        "node_id": block["id"],
        "component": spec.component,
        "name": spec.component,
        "params": spec.model_dump(),
        "symbol": symbol,
        "timeframe": timeframe,
        "pane": "price" if spec.unit == "price_quote_per_base" else spec.unit,
        "unit": spec.unit,
        "provenance": "derived",
        "formula_version": spec.formula_version,
        "availability_model": "historical_ideal_close_v1",
        "history_bars": window,
        "points": points,
    }
