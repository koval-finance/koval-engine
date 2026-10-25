"""One mutable owner of frozen historical level arming and consumption."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite

from koval.exchanges.base import timeframe_ms
from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import LevelSnapshotV1, MarketEvent, NumericValueV1
from koval.strategy.graph.evidence import observe
from koval.strategy.graph.level_identity import (
    LEVEL_FORMULA_VERSION,
    canonical_hash,
    decision_cursor,
    frozen_level_id,
    frozen_source_hash,
    opportunity_id,
    primary_context_hash,
)
from koval.strategy.graph.level_state import (
    LevelSetupParamsV1,
    LevelSetupStateV1,
    validate_level_setup_state,
)
from koval.strategy.graph.node import NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import register_node
from koval.strategy.helpers.levels import (
    breakout_retested,
    level_center,
    level_invalidated,
    sweep_reclaimed,
)
from koval.strategy.nodes.cross import cross_direction
from koval.strategy.nodes.value_contracts import first, scalar_status


def _primary_status(ctx):
    if not ctx.symbol or not ctx.timeframe:
        return "missing", "missing_identity"
    try:
        interval = timeframe_ms(ctx.timeframe)
    except ValueError:
        return "invalid", "unsupported_timeframe"
    if ctx.decision_timestamp_ms is None:
        return "missing", "missing_timing"
    if ctx.decision_timestamp_ms < ctx.timestamp_ms + interval:
        return "missing", "future_availability"
    if ctx.decision_timestamp_ms >= ctx.timestamp_ms + 2 * interval:
        return "stale", "stale_primary_bar"
    prices = (ctx.open, ctx.high, ctx.low, ctx.close)
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) and v > 0
        for v in prices
    ):
        return "invalid", "invalid_primary_bar"
    if ctx.low > min(ctx.open, ctx.close) or ctx.high < max(ctx.open, ctx.close):
        return "invalid", "invalid_primary_ohlc"
    if ctx.closes is None or len(ctx.closes) == 0 or ctx.history_start_ms is None:
        return "missing", "missing_primary_history"
    if ctx.history_start_ms + (len(ctx.closes) - 1) * interval != ctx.timestamp_ms:
        return "invalid", "noncontiguous_primary_history"
    if float(ctx.closes[-1]) != ctx.close:
        return "invalid", "primary_close_mismatch"
    return "ready", None


def _inputs(inputs):
    if set(inputs) - {"trigger", "lower", "upper"}:
        raise ValueError("unknown level input port")
    values = [first(inputs, name) for name in ("trigger", "lower", "upper")]
    for value, kind in zip(values, (MarketEvent, NumericValueV1, NumericValueV1), strict=True):
        if value is not None:
            if not isinstance(value, kind):
                raise ValueError(f"level input requires {kind.__name__}")
            # Revalidate model_copy payloads and mutable legacy events as well.
            kind.model_validate(value.model_dump())
    return values


def _payload_hash(ctx, inputs):
    # Invalid primary prices remain fingerprintable so a gap/missing bar can
    # invalidate the setup. They can never participate in a price predicate.
    prices = [
        v if isinstance(v, (int, float)) and isfinite(v) else repr(v)
        for v in (ctx.open, ctx.high, ctx.low, ctx.close)
    ]
    return canonical_hash(
        [
            ctx.bar_index,
            ctx.timestamp_ms,
            ctx.decision_timestamp_ms,
            ctx.symbol,
            ctx.timeframe,
            prices,
            _primary_status(ctx),
            [None if v is None else v.model_dump() for v in inputs],
        ]
    )


def _cursor(ctx, state, payload_hash):
    for name in ("bar_index", "timestamp_ms"):
        value = getattr(ctx, name)
        if type(value) is not int or value < 0:
            raise ValueError("level requires nonnegative integer primary cursors")
    if ctx.decision_timestamp_ms is not None and (
        type(ctx.decision_timestamp_ms) is not int or ctx.decision_timestamp_ms < 0
    ):
        raise ValueError("level requires an integer decision clock")
    if not state:
        return False, False
    if (state["symbol"], state["timeframe"]) != (ctx.symbol, ctx.timeframe):
        raise ValueError("level primary identity mismatch")
    if ctx.timestamp_ms < state["last_timestamp_ms"] or ctx.bar_index < state["last_bar_index"]:
        raise ValueError("level cursor moved backwards")
    if ctx.timestamp_ms == state["last_timestamp_ms"]:
        if ctx.bar_index != state["last_bar_index"] or payload_hash != state["last_payload_hash"]:
            raise ValueError("level duplicate cursor payload conflict")
        return True, False
    if (
        ctx.decision_timestamp_ms is not None
        and state["last_decision_timestamp_ms"] is not None
        and ctx.decision_timestamp_ms < state["last_decision_timestamp_ms"]
    ):
        raise ValueError("level decision clock moved backwards")
    if ctx.bar_index <= state["last_bar_index"]:
        raise ValueError("level primary index moved backwards")
    try:
        interval = timeframe_ms(ctx.timeframe)
    except ValueError:
        return False, False
    return False, (
        ctx.timestamp_ms == state["last_timestamp_ms"] + interval
        and ctx.bar_index == state["last_bar_index"] + 1
    )


def _trigger_status(ctx, event, state):
    if event is None:
        return None, "no_trigger"
    if not event.source_node_id or not event.kind or event.bar_index < 0 or event.timestamp_ms < 0:
        raise ValueError("malformed level trigger identity")
    key = canonical_hash(event.model_dump())
    if event.timestamp_ms != ctx.timestamp_ms or event.bar_index != ctx.bar_index:
        return None, "stale_or_future_trigger"
    if state.get("last_trigger_timestamp_ms") is not None and (
        event.timestamp_ms <= state["last_trigger_timestamp_ms"]
        or event.bar_index <= state["last_trigger_bar"]
    ):
        return None, "replayed_trigger"
    return key, None


def _unavailable(ctx, node_id, config_hash, readiness, reason):
    return LevelSnapshotV1(
        bar_index=ctx.bar_index,
        timestamp_ms=ctx.timestamp_ms,
        source_node_id=node_id,
        symbol=ctx.symbol,
        timeframe=ctx.timeframe,
        config_hash=config_hash,
        level_id=None,
        lower=None,
        upper=None,
        center=None,
        lower_source=None,
        upper_source=None,
        source_hash=None,
        trigger_key=None,
        trigger_source_node_id=None,
        trigger_timestamp_ms=None,
        trigger_bar=None,
        detected_at_ms=None,
        armed_bar=None,
        expires_bar=None,
        status="unavailable",
        readiness=readiness,
        reason=reason,
        terminal_bar=None,
        terminal_timestamp_ms=None,
        consumed_at_ms=None,
        consumption_cursor=None,
        event_id=None,
    )


def _arm(ctx, params, node_id, event, key, lower, upper, config_hash):
    if params.mode == "breakout_retest" and upper is not None:
        raise ValueError("breakout_retest requires a single lower level; omit upper")
    operands = [lower] if upper is None else [lower, upper]
    readiness, reason = scalar_status(ctx, operands, price_only=True)
    if readiness != "ready":
        return _unavailable(ctx, node_id, config_hash, readiness, reason)
    top = lower.value if upper is None else upper.value
    if lower.value <= 0 or top < lower.value:
        return _unavailable(ctx, node_id, config_hash, "invalid", "invalid_bounds")
    if params.mode == "breakout_retest" and not isfinite(
        lower.value * (1 + params.tolerance_pct / 100)
    ):
        return _unavailable(ctx, node_id, config_hash, "invalid", "nonfinite_retest_threshold")
    source_hash = frozen_source_hash(lower, upper)
    return LevelSnapshotV1(
        bar_index=ctx.bar_index,
        timestamp_ms=ctx.timestamp_ms,
        source_node_id=node_id,
        symbol=ctx.symbol,
        timeframe=ctx.timeframe,
        config_hash=config_hash,
        level_id=frozen_level_id(node_id, key, source_hash),
        lower=lower.value,
        upper=top,
        center=level_center(lower.value, top),
        lower_source=lower.model_copy(deep=True),
        upper_source=None if upper is None else upper.model_copy(deep=True),
        source_hash=source_hash,
        trigger_key=key,
        trigger_source_node_id=event.source_node_id,
        trigger_timestamp_ms=event.timestamp_ms,
        trigger_bar=event.bar_index,
        detected_at_ms=ctx.decision_timestamp_ms,
        armed_bar=ctx.bar_index,
        expires_bar=ctx.bar_index + params.expires_after_bars,
        status="armed",
        readiness="ready",
        reason=None,
        terminal_bar=None,
        terminal_timestamp_ms=None,
        consumed_at_ms=None,
        consumption_cursor=None,
        event_id=None,
    )


def _service(ctx, params, level, state, consecutive, readiness, reason):
    updated = level.model_dump() | {"bar_index": ctx.bar_index, "timestamp_ms": ctx.timestamp_ms}
    predicate = params.mode
    if ctx.bar_index > level.expires_bar:
        status, why, passed = "expired", "expiry_elapsed", False
    elif not consecutive or readiness != "ready" or not state["last_primary_valid"]:
        status, why, passed = "invalidated", reason or "primary_discontinuity", False
    elif level_invalidated(ctx.open, ctx.close, level.lower, params.invalidation_buffer_pct):
        status, why, passed = "invalidated", "price_below_invalidation", False
    else:
        if params.mode == "sweep_reclaim":
            passed = sweep_reclaimed(ctx.low, ctx.close, level.lower)
        elif params.mode == "breakout_retest":
            passed = breakout_retested(ctx.low, ctx.close, level.lower, params.tolerance_pct)
        else:
            passed = (
                cross_direction(state["last_close"], level.upper, ctx.close, level.upper) == "up"
            )
        status, why = ("consumed", "opportunity_consumed") if passed else ("armed", None)
    updated.update(status=status, reason=why)
    event = None
    if status != "armed":
        updated.update(terminal_bar=ctx.bar_index, terminal_timestamp_ms=ctx.timestamp_ms)
    if passed:
        cursor = decision_cursor(ctx)
        event_id = opportunity_id(level.level_id, cursor)
        updated.update(
            consumed_at_ms=ctx.decision_timestamp_ms, consumption_cursor=cursor, event_id=event_id
        )
        event = MarketEvent(
            bar_index=ctx.bar_index,
            timestamp_ms=ctx.timestamp_ms,
            source_node_id=level.source_node_id,
            kind="level_opportunity",
            direction="bullish",
            price_levels=[level.lower, level.upper],
            metadata={
                "event_id": event_id,
                "level_id": level.level_id,
                "consumption_cursor": cursor,
                "decision_timestamp_ms": ctx.decision_timestamp_ms,
                "formula_version": LEVEL_FORMULA_VERSION,
                "mode": params.mode,
                "lower": level.lower,
                "upper": level.upper,
                "center": level.center,
                "source_hash": level.source_hash,
                "trigger_key": level.trigger_key,
                "predicate": predicate,
                "primary": {"open": ctx.open, "low": ctx.low, "close": ctx.close},
                "previous_close": state["last_close"],
                "previous_cursor": [
                    state["last_bar_index"],
                    state["last_timestamp_ms"],
                    state["last_decision_timestamp_ms"],
                ],
            },
        )
    return LevelSnapshotV1.model_validate(updated), event


def level_setup_evaluator(params, *, node_id="state.level_setup", graph_binding_hash=None):
    """Bind a level owner to its graph instance ID; direct node callers use the type ID."""
    config_hash = canonical_hash(params.semantic_config())

    def evaluate(ctx, inputs, state):
        validate_level_setup_state(
            params, state, node_id=node_id, graph_binding_hash=graph_binding_hash
        )
        event, lower, upper = values = _inputs(inputs)
        payload_hash = _payload_hash(ctx, values)
        duplicate, consecutive = _cursor(ctx, state, payload_hash)
        if duplicate:
            level = LevelSnapshotV1.model_validate(deepcopy(state["output"]))
            observe(
                ctx,
                inputs=[],
                values={"level": level.model_dump(), "reason": "duplicate_cursor"},
                result="recorded",
            )
            return {"level": level, "event": None}
        key, trigger_reason = _trigger_status(ctx, event, state)
        readiness, reason = _primary_status(ctx)
        previous = LevelSnapshotV1.model_validate(state["output"]) if state else None
        emitted = None
        used = []
        if previous is not None and previous.status == "armed":
            level, emitted = _service(ctx, params, previous, state, consecutive, readiness, reason)
            trigger_reason = "trigger_ignored_while_armed" if key else trigger_reason
        elif previous is not None and previous.status != "unavailable":
            level = LevelSnapshotV1.model_validate(
                previous.model_dump()
                | {"bar_index": ctx.bar_index, "timestamp_ms": ctx.timestamp_ms}
            )
            if key is not None and readiness == "ready":
                candidate = _arm(ctx, params, node_id, event, key, lower, upper, config_hash)
                if candidate.status == "armed":
                    level = candidate
                    used = [v for v in values if v is not None]
                else:
                    trigger_reason = candidate.reason
            elif key is not None:
                trigger_reason = reason
        elif key is not None and readiness == "ready":
            level = _arm(ctx, params, node_id, event, key, lower, upper, config_hash)
            used = [v for v in values if v is not None]
        else:
            level = _unavailable(
                ctx,
                node_id,
                config_hash,
                readiness if readiness != "ready" else "missing",
                reason or trigger_reason,
            )
        arming_evidence = deepcopy(state.get("arming_evidence", []))
        if level.status == "armed" and level.armed_bar == ctx.bar_index:
            arming_evidence = (
                []
                if ctx.evidence is None
                else ctx.evidence._unique(
                    [row for value in used for row in ctx.evidence.observations(value)]
                )
            )
        data = dict(
            version=1,
            formula_version=LEVEL_FORMULA_VERSION,
            config_hash=config_hash,
            node_id=node_id,
            graph_binding_hash=graph_binding_hash,
            context_hash=primary_context_hash(ctx),
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            last_timestamp_ms=ctx.timestamp_ms,
            last_bar_index=ctx.bar_index,
            last_decision_timestamp_ms=ctx.decision_timestamp_ms,
            last_payload_hash=payload_hash,
            last_close=ctx.close if readiness == "ready" else None,
            last_primary_valid=readiness == "ready",
            last_trigger_key=key or state.get("last_trigger_key"),
            last_trigger_timestamp_ms=ctx.timestamp_ms
            if key
            else state.get("last_trigger_timestamp_ms"),
            last_trigger_bar=ctx.bar_index if key else state.get("last_trigger_bar"),
            arming_evidence=arming_evidence,
            arming_evidence_hash=canonical_hash(arming_evidence),
            output=level.model_dump(),
        )
        data["integrity_hash"] = canonical_hash(data)
        saved = LevelSetupStateV1.model_validate(data)
        # Consume before returning: downstream policy veto cannot retry this setup.
        state.clear()
        state.update(saved.model_dump())
        observe(ctx, inputs=used)
        if not used and arming_evidence and ctx.evidence is not None:
            ctx.evidence.retain(
                arming_evidence,
                list(dict.fromkeys(row["runtime_node_id"] for row in arming_evidence)),
            )
        observe(
            ctx,
            values={
                "level": level.model_dump(),
                "trigger_reason": trigger_reason,
                "primary_readiness": readiness,
                "primary_reason": reason,
            },
            predicate=params.mode,
            result="passed"
            if emitted
            else "unavailable"
            if level.status == "unavailable"
            else "recorded",
        )
        return {"level": level, "event": emitted}

    return evaluate


register_node(
    NodeSpec(
        "state.level_setup",
        Domain.STATE,
        "Frozen Level Setup",
        "Freeze causal price bounds on a trigger; consume one later bullish opportunity.",
        LevelSetupParamsV1,
        {
            "trigger": PortSpec("trigger", (MarketEvent,)),
            "lower": PortSpec("lower", (NumericValueV1,)),
            "upper": PortSpec("upper", (NumericValueV1,), required=False),
        },
        {
            "level": PortSpec("level", (LevelSnapshotV1,)),
            "event": PortSpec("event", (MarketEvent,), required=False),
        },
        level_setup_evaluator,
    )
)
