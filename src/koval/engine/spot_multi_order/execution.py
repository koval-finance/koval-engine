"""Atomic observation -> accounting -> pure recipe decisions; no dispatch."""

from koval.engine.spot_numbers import exact, number

from .account import (
    apply_spot_event,
    apply_spot_fee_valuation,
    cancel_spot_order,
    reconcile_spot_state,
    submit_spot_order,
)
from .controllers import dca_decide, grid_decide, terminal_decisions, trigger_terminal
from .models import (
    SpotClosedBarPayloadV1,
    SpotDecisionV1,
    SpotObservationV1,
    SpotPricePayloadV1,
    SpotTransitionV1,
    spot_content_hash,
)


@exact
def advance_spot(state, observation):
    observation = SpotObservationV1.model_validate(observation)
    digest = spot_content_hash(observation)
    prior = state.observations.get(observation.observation_id)
    if prior:
        if prior != digest:
            raise ValueError("conflicting observation ID")
        return SpotTransitionV1(
            state=state.model_copy(deep=True), decisions=SpotDecisionV1(), records=[]
        )
    if observation.sequence != state.observation_sequence + 1:
        raise ValueError("Spot observation sequence mismatch")
    candidate = state.model_copy(deep=True)
    before = len(candidate.trace)
    candidate.observations[observation.observation_id] = digest
    candidate.observation_sequence = observation.sequence
    payload = observation.payload
    if observation.kind == "executions":
        if set(payload) != {"events"}:
            raise ValueError("invalid execution observation fields")
        for event in payload["events"]:
            candidate = apply_spot_event(candidate, event)
    elif observation.kind == "reconciliation":
        candidate = reconcile_spot_state(candidate, payload)
    elif observation.kind == "fee_valuation":
        candidate = apply_spot_fee_valuation(candidate, payload)
    elif observation.kind in {"price", "closed_bar"}:
        model = SpotPricePayloadV1 if observation.kind == "price" else SpotClosedBarPayloadV1
        payload = model.model_validate(payload).model_dump(mode="json", exclude_none=True)
        allowed = (
            {"price", "bar_index", "phase", "crossing_index", "bar_id"}
            if observation.kind == "price"
            else {
                "bar_index",
                "close",
                "start",
                "stop_hold",
                "stop_flatten",
                "ohlcv",
                "decision_timestamp_ms",
            }
        )
        if set(payload) - allowed:
            raise ValueError("unknown market observation fields")
        index = payload["bar_index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < candidate.bar_index:
            raise ValueError("stale/invalid Spot bar index")
        if observation.kind == "price":
            bar_key = str(index)
            bar_id = payload.get("bar_id", candidate.bar_ids.get(bar_key, bar_key))
            if bar_key in candidate.bar_ids and candidate.bar_ids[bar_key] != bar_id:
                raise ValueError("conflicting price candle identity")
            candidate.bar_ids[bar_key] = bar_id
            phase = ["open", "descending", "ascending", "close"].index(payload["phase"])
            crossing = payload.get(
                "crossing_index",
                candidate.price_crossing_index + 1 if candidate.price_bar == index else 1,
            )
            identity = f"{index}:{bar_id}:{crossing}"
            crossing_digest = spot_content_hash(
                {"price": payload["price"], "phase": payload["phase"]}
            )
            prior_crossing = candidate.crossing_digests.get(identity)
            if prior_crossing:
                if prior_crossing != crossing_digest:
                    raise ValueError("conflicting price crossing")
                return SpotTransitionV1(state=candidate, decisions=SpotDecisionV1(), records=[])
            if candidate.price_bar == index and (
                phase < candidate.price_phase or crossing <= candidate.price_crossing_index
            ):
                raise ValueError("causal price phase/crossing regression")
            if bar_key in candidate.closed_bar_digests:
                raise ValueError("price crossing after closed candle decision")
            candidate.crossing_digests[identity] = crossing_digest
            candidate.price_bar = index
            candidate.price_phase = phase
            candidate.price_crossing_index = crossing
        else:
            bar_key = str(index)
            closed_digest = spot_content_hash(payload)
            if bar_key in candidate.closed_bar_digests:
                if candidate.closed_bar_digests[bar_key] != closed_digest:
                    raise ValueError("conflicting closed candle identity")
                return SpotTransitionV1(state=candidate, decisions=SpotDecisionV1(), records=[])
            candidate.closed_bar_digests[bar_key] = closed_digest
        candidate.bar_index = index
        mark = number(payload["price"] if observation.kind == "price" else payload["close"])
        if mark <= 0:
            raise ValueError("Spot observation price must be positive")
        candidate.mark = mark
        if payload.get("stop_flatten") or payload.get("stop_hold"):
            trigger_terminal(
                candidate,
                "stop_and_flatten" if payload.get("stop_flatten") else "stop_and_hold",
                "signal",
                observation.observation_id,
            )
    elif observation.kind == "command":
        if set(payload) - {"command_id", "action", "reason"} or payload.get("action") not in {
            "stop_and_hold",
            "stop_and_flatten",
        }:
            raise ValueError("invalid terminal command")
        key = payload["command_id"]
        if key in candidate.commands and candidate.commands[key] != payload:
            raise ValueError("conflicting command ID")
        candidate.commands[key] = payload
        trigger_terminal(candidate, payload["action"], payload.get("reason", "manual"), key)
        candidate.trace.append({"kind": "terminal_command", **payload})
    decide = dca_decide if candidate.recipe.kind == "dca" else grid_decide
    decision = decide(
        candidate.controller, {"state": candidate, "kind": observation.kind, "payload": payload}
    )
    candidate.controller = decision.controller
    candidate.account.cycle_reserve = decision.cycle_reserve
    candidate.account.cleanup_reserve = decision.cleanup_reserve
    routing_base = candidate.model_copy(deep=True)
    try:
        for intent in decision.intents:
            if intent["kind"] == "submit":
                candidate = submit_spot_order(candidate, intent["intent"])
            elif intent["kind"] == "cancel":
                candidate = cancel_spot_order(
                    candidate,
                    intent["client_order_id"],
                    command_id=intent["command_id"],
                    generation=intent["generation"],
                )
            else:
                candidate.trace.append(intent)
    except ValueError as exc:
        if not routing_base.account.net_base:
            raise
        candidate = routing_base
        candidate.controller.refusal_reason = f"admission_failure:{exc}"
        trigger_terminal(candidate, "stop_and_flatten", "admission_failure")
        safe = []
        terminal_decisions(candidate, safe)
        candidate.trace.append({"kind": "containment", "reason": str(exc)})
        for intent in safe:
            if intent["kind"] == "cancel":
                candidate = cancel_spot_order(
                    candidate,
                    intent["client_order_id"],
                    command_id=intent["command_id"],
                    generation=intent["generation"],
                )
            elif intent["kind"] == "submit":
                candidate = submit_spot_order(candidate, intent["intent"])
        decision = SpotDecisionV1(
            intents=safe,
            controller=candidate.controller,
            cycle_reserve=candidate.account.cycle_reserve,
            cleanup_reserve=candidate.account.cleanup_reserve,
        )
    return SpotTransitionV1(state=candidate, decisions=decision, records=candidate.trace[before:])
