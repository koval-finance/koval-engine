"""Production archive verification against a separately trusted host head."""

from copy import deepcopy

from koval.engine.run_identity import content_sha256
from koval.engine.runtime_journal import verify_runtime_records


def verify_spot_archive(bundle, context, host):
    """Verify complete segments and all durable operation/event identities.

    Host verification is mandatory: a truncated, internally consistent prefix
    cannot establish that no newer intent was dispatched. No force path exists.
    """
    try:
        segments = bundle["archive_segments"]
        state = bundle["execution"]
        checkpoint = bundle["archive_checkpoint"]
        if not segments or state["context"] != context or state["version"] != 2:
            raise ValueError("Spot archive binding mismatch")
        records, segment_ids = [], set()
        prior = None
        for segment in segments:
            head, values = segment["checkpoint"], segment["records"]
            if (
                head["version"] != "koval_runtime_checkpoint_v1"
                or head["archive_enabled"] is not True
                or head["session_id"] in segment_ids
                or head["sequence"] != len(values)
                or head["record_sha256"] != verify_runtime_records(values)
                or (values and values[0]["session_id"] != head["session_id"])
            ):
                raise ValueError("Spot archive checkpoint mismatch")
            if any(r.get("spot_context") != context for r in values):
                raise ValueError("Spot archive mixed execution context")
            if prior is not None and not any(
                r["kind"] == "spot_restore" and r["payload"]["source_checkpoint"] == prior
                for r in values
            ):
                raise ValueError("Spot archive segment linkage missing")
            segment_ids.add(head["session_id"])
            records.extend(values)
            prior = head
        if checkpoint != prior:
            raise ValueError("Spot archive final checkpoint mismatch")
        _verify_facts(state, records)
        verifier = getattr(host, "verify_spot_restore", None)
        if not callable(verifier) or verifier(deepcopy(bundle)) != content_sha256(bundle):
            raise ValueError("Spot archive requires verified authoritative host head")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("incomplete Spot archive") from exc


def _verify_facts(state, records):
    for kind, field, key in (
        ("order_intent", "intents", "client_order_id"),
        ("fill", "events", "event_id"),
        ("spot_protection_intent", "protection_intents", "stop_client_order_id"),
    ):
        durable = [r["payload"] for r in records if r["kind"] == kind]
        values = state[field]
        if (
            len({v[key] for v in durable}) != len(durable)
            or len({v[key] for v in values}) != len(values)
            or len(durable) != len(values)
        ):
            raise ValueError("Spot archive omits durable operation/event")
        for value, recorded in zip(values, durable, strict=True):
            if any(content_sha256(recorded.get(k)) != content_sha256(v) for k, v in value.items()):
                raise ValueError("Spot archive changed operation/event order or payload")
    _verify_attempt_projections(state, records)
    finished = [r["payload"] for r in records if r["kind"] == "spot_finish"]
    if len(finished) > 1 or (finished[-1] if finished else None) != state["terminal"]:
        raise ValueError("Spot archive terminal disposition mismatch")
    attempts = {
        r["payload"]["attempt_id"]: r["payload"]
        for r in records
        if r["kind"] == "spot_disposal_attempt"
    }
    if set(attempts) != set(state["disposal_attempts"]):
        raise ValueError("Spot archive disposal attempts mismatch")
    for key, attempt in state["disposal_attempts"].items():
        if any(
            attempt[field] != attempts[key][field]
            for field in ("payload", "request", "authorization", "intent")
        ):
            raise ValueError("Spot archive changed disposal attempt")


def _verify_attempt_projections(state, records):
    from dataclasses import fields

    from koval.engine.broker import BrokerOrderAck
    from koval.engine.spot_contract import SpotProtection

    entries = [i for i in state["intents"] if i["role"] == "entry"]
    entry_id = entries[-1]["client_order_id"] if entries else None
    closes = [
        i["client_order_id"]
        for i in state["intents"]
        if i["role"] == "flatten" and "authorization" not in i["metadata"]
    ]
    close_id = closes[-1] if closes else None
    cancellations = sorted(
        {r["payload"]["client_order_id"] for r in records if r["kind"] == "spot_cancel_entry"}
    )
    close_attempted = any(r["kind"] == "spot_cancel_all" for r in records)
    if (
        state["entry_id"] != entry_id
        or state["close_id"] != close_id
        or state["close_attempted"] is not close_attempted
        or state["cancel_attempted"] != cancellations
    ):
        raise ValueError("Spot archive changed durable entry/close/cancel identity")
    for kind, field, model in (
        ("order_ack", "order_acks", BrokerOrderAck),
        ("spot_protection_ack", "protection_acks", SpotProtection),
    ):
        values = [
            {f.name: r["payload"][f.name] for f in fields(model)}
            for r in records
            if r["kind"] == kind
        ]
        if content_sha256(state[field]) != content_sha256(values):
            raise ValueError("Spot archive changed acknowledgement history")
    for attempt in state["disposal_attempts"].values():
        acks = [
            a
            for a in state["order_acks"]
            if a["client_order_id"] == attempt["intent"]["client_order_id"]
        ]
        expected_ack = acks[-1] if acks else None
        if content_sha256(attempt["ack"]) != content_sha256(expected_ack) or attempt["status"] != (
            "acknowledged" if acks else "unknown"
        ):
            raise ValueError("Spot archive changed disposal acknowledgement")
    closed = sorted(
        {r["payload"]["entry_client_order_id"] for r in records if r["kind"] == "trade"}
    )
    if state["closed_entries"] != closed:
        raise ValueError("Spot archive changed completed trades")
    _verify_cycle(state, records, entry_id)


def _verify_cycle(state, records, entry_id):
    from koval.engine.spot_account import SpotLedger, decimal_value, spot_decimal_context
    from koval.engine.spot_contract import SpotFee, SpotFill, SpotIdentity

    @spot_decimal_context
    def verify():
        ledger = SpotLedger(SpotIdentity(**state["identity"]), state["capital"])
        intents = {i["client_order_id"]: i for i in state["intents"]}
        for record in records:
            value = record["payload"]
            if record["kind"] == "order_intent" and value["client_order_id"] == entry_id:
                break
            if record["kind"] == "fill":
                fields = {k: value[k] for k in state["events"][0]}
                fields["fees"] = tuple(SpotFee(**f) for f in fields["fees"])
                ledger.apply(
                    SpotFill(**fields),
                    stop_price=(
                        intents[value["client_order_id"]]["stop_price"]
                        if value["role"] == "entry"
                        else "0"
                    ),
                )
        baseline = {
            "realized": str(ledger.realized),
            "fees": str(ledger.valued_fees),
            "event_count": len(ledger.events),
        }
        if state["cycle_baseline"] != baseline:
            raise ValueError("Spot archive changed cycle accounting baseline")
        outside = any(
            f["client_order_id"] == entry_id
            and decimal_value(f["quantity"] or "0")
            and not decimal_value(intents[entry_id]["stop_price"])
            < decimal_value(f["price"])
            < decimal_value(intents[entry_id]["target_price"])
            for f in state["events"]
        )
        if state["outside_bracket"] is not bool(outside):
            raise ValueError("Spot archive changed frozen bracket breach")

    verify()
