"""Explicit host authority for bounded disposal and immutable custody receipts.

The injected host authenticates the human, atomically consumes grants, verifies
fresh evidence and durable archive heads. This module contains no auth service,
credentials, transport, or network retry. A broker must separately require a
durable host permit for every mutating method, including safety actions.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Protocol

from koval.engine.broker import BrokerOrderIntent
from koval.engine.client_order_id import make_client_order_id
from koval.engine.run_identity import content_sha256
from koval.engine.spot_account import decimal_value, spot_decimal_context


@dataclass(frozen=True)
class SpotControlReceipt:
    """Trusted host response, binding the canonical request to a consumed grant.

    Quantity/notional ceilings use a fresh host-verified reference price. They
    bound the requested disposal, not the eventual MARKET execution price.
    """

    request_sha256: str
    receipt_id: str
    grant_id: str
    quantity: str = "0"
    max_quantity: str = "0"
    max_notional: str = "0"
    reference_price: str = "0"
    protection_disposition: str = "retain"
    retained_order_ids: tuple[str, ...] = ()


class SpotHost(Protocol):
    def authorize_spot_control(self, request: dict) -> SpotControlReceipt:
        """Verify immutable evidence and consume exact human authority durably."""
        ...

    def verify_spot_restore(self, bundle: dict) -> str:
        """Compare with authoritative durable head, return canonical bundle hash.

        Refuse missing/unacknowledged suffixes, foreign identity or unproved
        ownership. A self-consistent hash chain cannot prove its own tail.
        """
        ...


class SpotControls:
    def __init__(self, execution):
        self.execution = execution
        self.terminal: dict | None = None
        self.disposals: dict[str, dict] = {}

    def _production(self):
        if not self.execution.production:
            raise ValueError("Spot control requires explicit production contract")
        if self.execution.persistence_failed:
            raise RuntimeError("Spot storage failed; restore a fresh engine")

    def _request(self, action, **values):
        spot = self.execution
        return {
            "schema": "koval_spot_control_request_v2",
            "action": action,
            "context": deepcopy(spot.context),
            "state_hash": spot.observation_hash(),
            "base_quantity": str(spot.ledger.quantity),
            "open_order_ids": list(spot.last_report.open_order_ids),
            **values,
        }

    def _authorize(self, host, request):
        method = getattr(host, "authorize_spot_control", None)
        if not callable(method):
            raise RuntimeError("verified Spot host authorization required")
        receipt = method(deepcopy(request))
        if (
            not isinstance(receipt, SpotControlReceipt)
            or receipt.request_sha256 != content_sha256(request)
            or not receipt.receipt_id
            or not receipt.grant_id
        ):
            raise ValueError("Spot host receipt binding mismatch")
        prior = [v["authorization"] for v in self.disposals.values()]
        if self.terminal:
            prior.append(self.terminal["authorization"])
        if any(
            r["grant_id"] == receipt.grant_id or r["receipt_id"] == receipt.receipt_id
            for r in prior
        ):
            raise ValueError("Spot host grant/receipt already used")
        return receipt

    def _terminal_evidence(self, *, retain_protection=False):
        spot = self.execution
        report = spot.last_report
        required = set(spot.intents)
        list_ids = set()
        for intent in spot.protection_intents:
            children = {intent.stop_client_order_id, intent.target_client_order_id}
            if retain_protection and children == set(report.open_order_ids):
                continue
            required.update(children)
            list_ids.add(intent.metadata["list_client_order_id"])
        if (
            report.pending_entry_ids
            or report.unknown_request_ids
            or not report.fees_complete
            or not required.issubset(report.terminal_order_ids)
            or not list_ids.issubset(report.terminal_list_ids)
        ):
            raise RuntimeError("Spot predecessors/fees must be known terminal")
        if not retain_protection and report.open_order_ids:
            raise RuntimeError("Spot competing working orders")

    @spot_decimal_context
    def retry_close(self, host, *, attempt_id, expected_state_hash):
        self._production()
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise ValueError("Spot disposal attempt_id required")
        payload = {"attempt_id": attempt_id, "expected_state_hash": expected_state_hash}
        if attempt_id in self.disposals:
            saved = self.disposals[attempt_id]
            if saved["payload"] != payload:
                raise ValueError("Spot disposal payload changed")
            return deepcopy(saved)
        spot = self.execution
        if self.terminal:
            raise RuntimeError("Spot terminal disposition forbids disposal")
        spot.observe()
        if expected_state_hash != spot.observation_hash():
            raise ValueError("stale Spot observation hash")
        self._terminal_evidence()
        exits = [
            f
            for f in list(spot.ledger.events.values())[spot.cycle_baseline["event_count"] :]
            if f.role != "entry" and decimal_value(f.quantity or "0")
        ]
        if not exits or not spot.halted or not spot.ledger.quantity:
            raise RuntimeError("proved terminal partial-exit remainder required")
        free = decimal_value(spot.last_report.base_free)
        if free != spot.ledger.quantity:
            raise RuntimeError("confirmed free managed remainder required")
        request = self._request("retry_close", **payload)
        receipt = self._authorize(host, request)
        quantity = decimal_value(receipt.quantity, positive=True)
        if (
            quantity > free
            or quantity > decimal_value(receipt.max_quantity, positive=True)
            or quantity * decimal_value(receipt.reference_price, positive=True)
            > decimal_value(receipt.max_notional, positive=True)
        ):
            raise ValueError("Spot disposal exceeds authorized ceilings")
        name = "spot-disposal-" + attempt_id
        intent = BrokerOrderIntent(
            spot.session_id,
            name,
            make_client_order_id(spot.session_id, name, spot.id_namespace, "flatten"),
            spot.identity.symbol,
            "sell",
            "market",
            str(quantity),
            spot.identity.target,
            role="flatten",
            metadata={"spot_context": deepcopy(spot.context), "authorization": asdict(receipt)},
        )
        saved = {
            "payload": payload,
            "request": request,
            "authorization": asdict(receipt),
            "intent": asdict(intent),
            "status": "unknown",
            "ack": None,
        }
        # Reservation is durable before the intent or broker call. Unknown outcomes
        # remain reserved forever; replay returns this receipt without a send.
        spot._write("spot_disposal_attempt", {"attempt_id": attempt_id, **saved})
        self.disposals[attempt_id] = saved
        try:
            spot._write("order_intent", asdict(intent))
            spot.intents[intent.client_order_id] = intent
            ack = spot.broker.submit_exit(intent)
            spot._ack(ack, intent)
            saved.update(status="acknowledged", ack=asdict(ack))
            spot._save()
        except Exception:
            spot._halt()
            raise
        return deepcopy(saved)

    def finish(self, host, *, disposition, evidence_hash):
        self._production()
        if disposition not in {"closed_flat", "custody_transferred"}:
            raise ValueError("unsupported Spot disposition")
        if not isinstance(evidence_hash, str) or not evidence_hash.strip():
            raise ValueError("immutable Spot disposition evidence required")
        if self.terminal:
            if (
                self.terminal["disposition"] != disposition
                or self.terminal["evidence_hash"] != evidence_hash
            ):
                raise ValueError("Spot terminal disposition already used")
            return
        spot = self.execution
        spot.halt_entries("terminal disposition")
        spot.observe()
        if disposition == "closed_flat" and not spot.flat_confirmed:
            raise RuntimeError("closed_flat requires exact zero and no owned/unknown orders")
        self._terminal_evidence(retain_protection=disposition == "custody_transferred")
        report = spot.last_report
        if report.open_order_ids:
            spot._validate_protection(report.protection, report.open_order_ids)
        request = self._request("finish", disposition=disposition, evidence_hash=evidence_hash)
        receipt = self._authorize(host, request)
        if (
            receipt.protection_disposition not in {"retain", "cancel_owned"}
            or set(receipt.retained_order_ids) != set(report.open_order_ids)
            or (receipt.protection_disposition == "cancel_owned" and report.open_order_ids)
        ):
            raise ValueError("Spot protection disposition mismatch")
        terminal = {
            "disposition": disposition,
            "evidence_hash": evidence_hash,
            "request": request,
            "authorization": asdict(receipt),
            "inventory": str(spot.ledger.quantity),
            "protection": asdict(spot.protection) if spot.protection else None,
        }
        spot._write("spot_finish", terminal)
        self.terminal = terminal
        spot._save()
