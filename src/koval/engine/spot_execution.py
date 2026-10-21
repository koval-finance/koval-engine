"""Serialized Spot lifecycle driven exclusively by host-supplied venue evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from koval.engine.broker import BrokerOrderIntent, ProtectiveOrderIntent
from koval.engine.client_order_id import make_client_order_id
from koval.engine.spot_account import SpotLedger, decimal_value, spot_decimal_context
from koval.engine.spot_contract import (
    SPOT_TESTNET,
    SpotFee,
    SpotFill,
    SpotProtection,
    SpotReconciliation,
)


class SpotExecution:
    """Internal coordinator; hosts use LiveEngine's public Spot methods.

    All calls must be serialized by the host. There are no automatic mutation
    retries. A reconciliation report is a host attestation, not venue transport.
    """

    def __init__(self, broker, *, session_id, capital, binding, emit, account=None):
        self.broker = broker
        self.session_id = session_id
        self.identity = broker.spot_identity
        self.binding = deepcopy(binding)
        self.emit = emit
        self.ledger = SpotLedger(self.identity, capital, account)
        self.intents: dict[str, BrokerOrderIntent] = {}
        self.protection_intents: list[ProtectiveOrderIntent] = []
        self.protection: SpotProtection | None = None
        self.entry_id: str | None = None
        self.pending = False
        self.ready = False
        self.halted = False
        self.uncertain = False
        self.flat_confirmed = False
        self.close_attempted = False
        self.cancel_attempted: set[str] = set()
        self.persistence_failed = False
        self.last_revision: str | None = None
        self.quote_reserved = decimal_value("0")
        self._outside_bracket = False
        self._close_id = None
        self.closed_entries: set[str] = set()
        self.cycle_baseline = {"realized": "0", "fees": "0", "event_count": 0}
        self.restored = False

    def _write(self, kind, payload):
        try:
            self.emit(kind, deepcopy(payload))
        except Exception:
            self.persistence_failed = True
            self._halt()
            raise

    def _halt(self):
        self.halted = True
        self.uncertain = True
        self.ready = False
        self.flat_confirmed = False

    def _save(self):
        self._write("spot_state", self.export_state())

    @spot_decimal_context
    def submit(self, intent: BrokerOrderIntent):
        if self.halted or not self.ready or self.pending or self.ledger.quantity:
            raise RuntimeError("Spot entries halted or reconciliation required")
        if (
            intent.session_id != self.session_id
            or intent.symbol != self.identity.symbol
            or intent.target != SPOT_TESTNET
            or intent.side != "buy"
            or intent.order_type != "market"
            or intent.role != "entry"
            or intent.client_order_id in self.intents
        ):
            raise ValueError("invalid Spot market entry intent")
        quantity = decimal_value(intent.quantity, positive=True)
        price = decimal_value(intent.price, positive=True)
        stop = decimal_value(intent.stop_price, positive=True)
        target = decimal_value(intent.target_price, positive=True)
        if not stop < price < target or quantity * price > self.ledger.cash:
            raise ValueError("invalid Spot bracket or insufficient quote cash")
        self.flat_confirmed = False
        try:
            self.cycle_baseline = {
                "realized": str(self.ledger.realized),
                "fees": str(self.ledger.valued_fees),
                "event_count": len(self.ledger.events),
            }
            self._outside_bracket = False
            self._close_id = None
            self.close_attempted = False
            self._write("order_intent", asdict(intent))
            self.intents[intent.client_order_id] = deepcopy(intent)
            self.entry_id = intent.client_order_id
            self.pending = True
            ack = self.broker.submit_entry(intent)
            self._ack(ack, intent)
            if ack.status == "rejected":
                self._halt()
            self._save()
        except Exception:
            self._halt()
            raise

    def _ack(self, ack, intent):
        if (
            ack.session_id != self.session_id
            or ack.client_order_id != intent.client_order_id
            or ack.target != SPOT_TESTNET
            or ack.status not in {"accepted", "filled", "rejected"}
        ):
            raise ValueError("invalid Spot acknowledgement")
        self._write("order_ack", asdict(ack))

    def _known_role(self, client_id):
        if client_id in self.intents:
            return self.intents[client_id].role
        for intent in self.protection_intents:
            if client_id == intent.stop_client_order_id:
                return "stop"
            if client_id == intent.target_client_order_id:
                return "target"
        return None

    @spot_decimal_context
    def _consume(self, fills):
        # Hosts order execution deltas causally. Zero-quantity OCO terminal
        # events may precede the sibling execution in the same batch.
        for fill in fills:
            if (
                not isinstance(fill, SpotFill)
                or fill.session_id != self.session_id
                or fill.symbol != self.identity.symbol
                or self._known_role(fill.client_order_id) != fill.role
                or not fill.exchange_order_id
            ):
                raise ValueError("unattributed Spot fill")
            if fill.event_id in self.ledger.events:
                if self.ledger.events[fill.event_id] != fill:
                    raise ValueError("conflicting Spot event_id")
                continue
            if decimal_value(fill.quantity or "0"):
                current_ids = {self.entry_id, self._close_id}
                for protection in self.protection_intents:
                    if protection.entry_client_order_id == self.entry_id:
                        current_ids.update(
                            (protection.stop_client_order_id, protection.target_client_order_id)
                        )
                if fill.client_order_id not in current_ids:
                    raise ValueError("execution belongs to a retired Spot cycle")
            stop = "0"
            if fill.role == "entry":
                intent = self.intents[fill.client_order_id]
                stop = intent.stop_price
                total = sum(
                    decimal_value(f.quantity or "0")
                    for f in self.ledger.events.values()
                    if f.client_order_id == fill.client_order_id
                ) + decimal_value(fill.quantity or "0")
                if total > decimal_value(intent.quantity):
                    raise ValueError("Spot entry exceeds requested quantity")
            # Validate atomically before the durable event and the live account.
            candidate = deepcopy(self.ledger)
            candidate.apply(fill, stop_price=stop)
            self._write("fill", asdict(fill))
            self.ledger.apply(fill, stop_price=stop)
            if any(
                fee.asset not in {self.identity.base_asset, self.identity.quote_asset}
                and decimal_value(fee.quantity)
                for fee in fill.fees
            ):
                self.halted = True
            if fill.role == "entry" and decimal_value(fill.quantity or "0"):
                intent = self.intents[fill.client_order_id]
                if (
                    not decimal_value(intent.stop_price)
                    < decimal_value(fill.price)
                    < decimal_value(intent.target_price)
                ):
                    self._outside_bracket = True
                if fill.status != "filled":
                    self.halted = True

    def _report(self):
        report = self.broker.reconcile_spot(self.session_id, list(self.intents.values()))
        if (
            not isinstance(report, SpotReconciliation)
            or report.identity != self.identity
            or report.session_id != self.session_id
            or not report.complete
            or not report.revision
        ):
            raise RuntimeError("complete Spot reconciliation required")
        self._consume(report.fills)
        if (
            decimal_value(report.quote_balance) != self.ledger.cash
            or decimal_value(report.base_quantity) != self.ledger.quantity
        ):
            raise RuntimeError("Spot account reconciliation mismatch")
        if len(set(report.open_order_ids)) != len(report.open_order_ids):
            raise ValueError("duplicate Spot open order identity")
        active_orders = {self.entry_id, self._close_id}
        for intent in self.protection_intents:
            if intent.entry_client_order_id == self.entry_id:
                active_orders.update((intent.stop_client_order_id, intent.target_client_order_id))
        for client_id in report.open_order_ids:
            if self._known_role(client_id) is None or client_id not in active_orders:
                raise RuntimeError("unknown Spot open order")
        if any(
            self._known_role(client_id) != "entry" or client_id not in report.open_order_ids
            for client_id in report.pending_entry_ids
        ):
            raise RuntimeError("invalid pending Spot entry identity")
        if any(
            self._known_role(client_id) == "entry" and client_id not in report.pending_entry_ids
            for client_id in report.open_order_ids
        ):
            raise RuntimeError("pending Spot entry omitted from reconciliation")
        self.pending = bool(report.pending_entry_ids)
        reserved = decimal_value(report.quote_reserved)
        if reserved > self.ledger.cash or (reserved and not self.pending):
            raise RuntimeError("invalid Spot quote reservation")
        self.quote_reserved = reserved
        self.last_revision = report.revision
        self.flat_confirmed = (
            self.ledger.quantity == 0 and not report.open_order_ids and not self.pending
        )
        self._write("spot_reconciliation", asdict(report))
        if self.flat_confirmed:
            self._complete_trade()
        return report

    def reconcile(self):
        """Read-only restoration gate; it never submits or repairs orders."""
        if self.persistence_failed:
            raise RuntimeError("Spot storage failed; restore a fresh engine")
        try:
            report = self._report()
            if self.pending:
                raise RuntimeError("Spot entry pending; cancel/reconcile before resume")
            if self.ledger.quantity:
                self._validate_protection(report.protection, report.open_order_ids)
                self.protection = report.protection
            elif report.open_order_ids:
                raise RuntimeError("Spot orders remain after inventory is flat")
            else:
                self.protection = None
            self.uncertain = False
            self.ready = True
            self._save()
            return self.export_state()
        except Exception:
            self._halt()
            raise

    def poll(self):
        if self.persistence_failed:
            raise RuntimeError("Spot storage failed; restore a fresh engine")
        try:
            self._consume(self.broker.poll_fills(self.session_id))
            report = self._report()
            if self.pending and self.ledger.quantity:
                self.halted = True
                for client_id in report.pending_entry_ids:
                    if client_id not in self.cancel_attempted:
                        self._write("spot_cancel_entry", {"client_order_id": client_id})
                        self.cancel_attempted.add(client_id)
                        self.broker.cancel_entry(self.identity.symbol, self.session_id, client_id)
                report = self._report()
                if self.pending:
                    raise RuntimeError("Spot entry remains pending after cancellation")
            if self.pending:
                self._save()
                return
            if self.ledger.quantity == 0:
                if report.open_order_ids:
                    raise RuntimeError("Spot terminal orders are not reconciled")
                self.protection = None
            elif self._outside_bracket:
                self.close()
            elif report.protection is not None:
                try:
                    self._validate_protection(report.protection, report.open_order_ids)
                except Exception:
                    self.close()
                    raise
                self.protection = report.protection
            elif self.protection is not None or self.close_attempted:
                self.close()
            else:
                if self.restored or any(
                    p.entry_client_order_id == self.entry_id for p in self.protection_intents
                ):
                    self.close()
                else:
                    self._protect(report)
            self.uncertain = False
            self._save()
        except Exception:
            self._halt()
            raise

    @spot_decimal_context
    def _validate_protection(self, protection, open_order_ids):
        if not isinstance(protection, SpotProtection) or not self.protection_intents:
            raise RuntimeError("Spot linked protection unavailable")
        intent = self.protection_intents[-1]
        if (
            intent.entry_client_order_id != self.entry_id
            or protection.session_id != self.session_id
            or protection.symbol != self.identity.symbol
            or not protection.order_list_id
            or protection.stop_client_order_id != intent.stop_client_order_id
            or protection.target_client_order_id != intent.target_client_order_id
            or protection.stop_client_order_id == protection.target_client_order_id
            or set(open_order_ids) != {intent.stop_client_order_id, intent.target_client_order_id}
        ):
            raise RuntimeError("invalid Spot linked protection identity or prices")
        stop = decimal_value(protection.stop_price, positive=True)
        target = decimal_value(protection.target_price, positive=True)
        tick = decimal_value(protection.price_tick)
        requested_stop = decimal_value(intent.stop_price, positive=True)
        requested_target = decimal_value(intent.target_price, positive=True)
        if tick:
            valid_prices = (
                stop % tick == 0
                and target % tick == 0
                and 0 <= stop - requested_stop < tick
                and 0 <= requested_target - target < tick
            )
        else:
            valid_prices = stop == requested_stop and target == requested_target
        if not valid_prices or not stop < self.ledger.cost / self.ledger.quantity < target:
            raise RuntimeError("Spot protection changes requested price rules")
        quantity = decimal_value(protection.quantity, positive=True)
        dust = decimal_value(protection.dust_quantity)
        step = decimal_value(protection.quantity_step)
        if (
            quantity + dust != self.ledger.quantity
            or (step and (quantity % step != 0 or dust >= step))
            or (dust and (not protection.dust_reason or not step))
        ):
            raise RuntimeError("Spot protection does not cover confirmed net inventory")
        if dust:
            self.halted = True
        current = self.ledger.account.snapshot().open_position
        if current is not None and float(stop) > current.current_stop:
            self.ledger.account.on_stop_update(float(stop))

    def _protect(self, report):
        if report.open_order_ids or self.entry_id is None:
            raise RuntimeError("cannot protect Spot inventory with unreconciled orders")
        entry = self.intents[self.entry_id]
        number = len(self.protection_intents) + 1
        intent = ProtectiveOrderIntent(
            self.session_id,
            self.entry_id,
            make_client_order_id(
                self.session_id, f"spot-protection-{number}", SPOT_TESTNET, "stop"
            ),
            make_client_order_id(
                self.session_id, f"spot-protection-{number}", SPOT_TESTNET, "target"
            ),
            self.identity.symbol,
            "sell",
            str(self.ledger.quantity),
            entry.stop_price,
            entry.target_price,
            SPOT_TESTNET,
            reduce_only=False,
        )
        self._write("spot_protection_intent", asdict(intent))
        self.protection_intents.append(intent)
        try:
            ack = self.broker.place_spot_protection(intent)
            self._write("spot_protection_ack", asdict(ack))
            self._validate_protection(ack, (ack.stop_client_order_id, ack.target_client_order_id))
            # Reconciliation may already contain executions newer than the receipt.
            report = self._report()
            if self.flat_confirmed:
                self.protection = None
                return
            self._validate_protection(report.protection, report.open_order_ids)
            if report.protection.order_list_id != ack.order_list_id:
                raise RuntimeError("Spot protection acknowledgement disagrees with reconciliation")
            self.protection = report.protection
        except Exception:
            if not self.persistence_failed:
                self.close()
            raise

    def close(self):
        """One recorded emergency/stop attempt; only fresh evidence proves flat."""
        self.halted = True
        self.ready = False
        if self.persistence_failed:
            raise RuntimeError("Spot storage failed; host reconciliation required")
        try:
            if not self.close_attempted:
                self._write("spot_cancel_all", {"symbol": self.identity.symbol})
                self.close_attempted = True
                self.broker.cancel_all(self.identity.symbol, self.session_id)
            report = self._report()
            if self.pending or report.open_order_ids:
                raise RuntimeError("Spot cancellation unconfirmed; closure unconfirmed")
            if self.flat_confirmed:
                self.protection = None
                self.uncertain = False
                self._save()
                return
            # A previously submitted exit is never blindly submitted again.
            if self._close_id is None:
                close_name = f"spot-close-{len(self.intents)}"
                client_id = make_client_order_id(
                    self.session_id, close_name, SPOT_TESTNET, "flatten"
                )
                intent = BrokerOrderIntent(
                    self.session_id,
                    close_name,
                    client_id,
                    self.identity.symbol,
                    "sell",
                    "market",
                    str(self.ledger.quantity),
                    SPOT_TESTNET,
                    role="flatten",
                )
                self._write("order_intent", asdict(intent))
                self.intents[client_id] = intent
                self._close_id = client_id
                self._ack(self.broker.submit_exit(intent), intent)
            self._consume(self.broker.poll_fills(self.session_id))
            report = self._report()
            if not self.flat_confirmed:
                raise RuntimeError("Spot closure unconfirmed")
            self.protection = None
            self.uncertain = False
            self._save()
        except Exception:
            self._halt()
            raise

    @spot_decimal_context
    def _complete_trade(self):
        if self.entry_id is None or self.entry_id in self.closed_entries:
            return
        events = list(self.ledger.events.values())[self.cycle_baseline["event_count"] :]
        buys = [f for f in events if f.role == "entry" and decimal_value(f.quantity or "0")]
        sells = [f for f in events if f.role != "entry" and decimal_value(f.quantity or "0")]
        if not buys or not sells:
            return
        from decimal import Decimal

        gross = self.ledger.realized - Decimal(self.cycle_baseline["realized"])
        fees = self.ledger.valued_fees - Decimal(self.cycle_baseline["fees"])
        unknown = sorted(
            {
                fee.asset
                for f in events
                for fee in f.fees
                if decimal_value(fee.quantity)
                and fee.asset not in {self.identity.base_asset, self.identity.quote_asset}
            }
        )
        bought = sum(decimal_value(f.quantity) for f in buys)
        sold = sum(decimal_value(f.quantity) for f in sells)
        record = {
            "entry_client_order_id": self.entry_id,
            "direction": "long",
            "entry_price": float(
                sum(decimal_value(f.quantity) * decimal_value(f.price) for f in buys) / bought
            ),
            "exit_price": float(
                sum(decimal_value(f.quantity) * decimal_value(f.price) for f in sells) / sold
            ),
            "size": float(sold),
            "gross_entry_quantity": str(bought),
            "pnl": None if unknown else float(gross - fees),
            "gross_pnl": float(gross),
            "commission": float(fees),
            "valuation_complete": not unknown,
            "unvalued_fee_assets": unknown,
            "reason": sells[-1].role,
            "entry_timestamp_ms": buys[0].timestamp_ms,
            "exit_timestamp_ms": sells[-1].timestamp_ms,
            "flat_confirmed": True,
        }
        self._write("trade", record)
        self.closed_entries.add(self.entry_id)

    def export_state(self):
        return deepcopy(
            {
                "version": 1,
                "identity": asdict(self.identity),
                "session_id": self.session_id,
                "capital": str(self.ledger.capital),
                "binding": self.binding,
                "intents": [asdict(i) for i in self.intents.values()],
                "protection_intents": [asdict(i) for i in self.protection_intents],
                "events": [asdict(f) for f in self.ledger.events.values()],
                "entry_id": self.entry_id,
                "halted": self.halted,
                "uncertain": self.uncertain,
                "close_attempted": self.close_attempted,
                "cancel_attempted": sorted(self.cancel_attempted),
                "last_revision": self.last_revision,
                "close_id": self._close_id,
                "closed_entries": sorted(self.closed_entries),
                "cycle_baseline": self.cycle_baseline,
            }
        )

    def restore(self, state):
        """Restore durable facts only. A separate fresh reconcile is mandatory."""
        if (
            state.get("version") != 1
            or state.get("identity") != asdict(self.identity)
            or state.get("session_id") != self.session_id
            or state.get("binding") != self.binding
            or decimal_value(state.get("capital")) != self.ledger.capital
        ):
            raise ValueError("Spot restore binding mismatch")
        if self.intents or self.ledger.events:
            raise ValueError("Spot restore requires a fresh engine")
        self._halt()
        self.restored = True
        self._close_id = state["close_id"]
        self.closed_entries = set(state["closed_entries"])
        self.cycle_baseline = deepcopy(state["cycle_baseline"])
        self.intents = {i["client_order_id"]: BrokerOrderIntent(**i) for i in state["intents"]}
        self.protection_intents = [ProtectiveOrderIntent(**i) for i in state["protection_intents"]]
        self.entry_id = state["entry_id"]
        self.close_attempted = state["close_attempted"]
        self.cancel_attempted = set(state["cancel_attempted"])
        for event in state["events"]:
            value = {**event, "fees": tuple(SpotFee(**fee) for fee in event["fees"])}
            fill = SpotFill(**value)
            if (
                self._known_role(fill.client_order_id) != fill.role
                or fill.session_id != self.session_id
            ):
                raise ValueError("Spot restore contains unattributed fill")
            stop = self.intents[fill.client_order_id].stop_price if fill.role == "entry" else "0"
            self.ledger.apply(fill, stop_price=stop)
        # Restart always halts additions. Reconciliation can recover protection;
        # only explicit flat resume can authorize new entries.
