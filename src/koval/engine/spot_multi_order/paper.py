"""Dedicated paper driver using the same public reducer and recipe decisions."""

from copy import deepcopy
from decimal import Decimal

import numpy as np

from koval.engine.backtest_engine import ExecutionCapabilities
from koval.engine.history_window import DEFAULT_HISTORY_BARS
from koval.engine.spot_numbers import decimal_text, exact, number
from koval.exchanges.base import timeframe_ms
from koval.strategy.block_assembler import assemble_from_graph
from koval.strategy.graph.execution_capabilities import graph_execution_capabilities

from .account import paper_reconciliation
from .execution import advance_spot
from .matching import eligible, modeled_event, status_event
from .models import (
    FEATURES,
    TERMINAL,
    ZERO,
    SpotBindingV1,
    SpotObservationV1,
    SpotRecipeV1,
    spot_content_hash,
)
from .state import create_spot_session, export_spot_state, restore_spot_state, spot_execution_report


class SpotPaperSessionV1:
    """Serialized offline/paper OHLC session; no broker or transport injection."""

    target = "paper"

    def __init__(self, binding, graph, instrument, policy, *, on_record):
        binding = SpotBindingV1.model_validate(binding)
        required = graph_execution_capabilities(graph)
        if (
            binding.mode != "paper"
            or "spot_multi_order_v1" not in required
            or required - set(self.capabilities().features)
        ):
            raise ValueError("unsupported Spot paper execution capabilities/mode")
        if binding.graph_sha256 != spot_content_hash(graph) or not callable(on_record):
            raise ValueError("graph binding and synchronous on_record required")
        self._strategy = assemble_from_graph(graph)
        recipes = [b for b in graph["blocks"] if b["type"] in {"exec.spot_dca", "exec.spot_grid"}]
        if len(recipes) != 1:
            raise ValueError("exactly one Spot recipe required")
        recipe = SpotRecipeV1(
            kind=recipes[0]["type"].removeprefix("exec.spot_"), params=recipes[0]["params"]
        )
        self._state = create_spot_session(binding, recipe, instrument, policy)
        self._sink = on_record
        self._failed = False
        self._timestamp = 0
        self._fresh = True
        self._strategy.config = {"symbol": binding.symbol, "timeframe": binding.timeframe}

    @staticmethod
    def capabilities():
        return ExecutionCapabilities((2,), (*FEATURES, "spot_dca_v1", "spot_grid_v1"))

    @property
    def state(self):
        return self._state.model_copy(deep=True)

    def _persist(self, candidate, kind):
        if self._failed:
            raise RuntimeError("Spot storage failed; restore a fresh session")
        record = {
            "version": "koval_spot_paper_journal_v1",
            "session_id": candidate.binding.session_id,
            "sequence": self._state.journal_sequence + 1,
            "previous_sha256": self._state.journal_sha256,
            "kind": kind,
            "state": export_spot_state(candidate),
        }
        digest = spot_content_hash(record)
        record["sha256"] = digest
        try:
            self._sink(deepcopy(record))
        except Exception:
            self._failed = True
            raise
        candidate.journal_sequence = record["sequence"]
        candidate.journal_sha256 = digest
        self._state = candidate
        self._fresh = False

    def _observe(self, kind, payload):
        state = self._state
        seq = state.observation_sequence + 1
        observation = SpotObservationV1(
            observation_id=f"paper-o{seq}",
            sequence=seq,
            timestamp_ms=self._timestamp,
            kind=kind,
            payload=payload,
        )
        received = state.model_copy(deep=True)
        received.pending_observation = observation.model_dump(mode="json")
        self._persist(received, "observation_received")
        try:
            transition = advance_spot(self._state, observation)
        except Exception:
            self._failed = True
            raise
        transition.state.pending_observation = None
        self._persist(transition.state, kind)

    def _drain(self):
        for _ in range(512):
            if self._state.reconciliation_required or self._state.quarantine:
                return
            pending = next((o for o in self._state.orders.values() if o.status == "pending"), None)
            if pending:
                self._execution(status_event(self._state, pending, "accepted", self._timestamp))
                continue
            canceled = next(
                (o for o in self._state.orders.values() if o.status == "cancel_pending"), None
            )
            if canceled:
                self._execution(status_event(self._state, canceled, "canceled", self._timestamp))
                continue
            if any(
                o.status in TERMINAL and not o.terminal_reconciled
                for o in self._state.orders.values()
            ):
                self._observe(
                    "reconciliation", paper_reconciliation(self._state).model_dump(mode="json")
                )
                continue
            return
        raise RuntimeError("Spot transition drain did not converge")

    def _execution(self, event):
        self._observe("executions", {"events": [event.model_dump(mode="json")]})

    def _price(self, price, phase):
        self._observe(
            "price",
            {"price": decimal_text(price), "bar_index": self._state.bar_index, "phase": phase},
        )
        self._drain()

    def _match(self, key, reference, at_open):
        order = self._state.orders[key]
        event = modeled_event(
            self._state, order, reference, at_open=at_open, timestamp_ms=self._timestamp
        )
        if event is None:
            return
        # Persist input/capacity before accounting the simulated execution.
        if event.quantity:
            candidate = self._state.model_copy(deep=True)
            candidate.liquidity_used += event.quantity
            self._persist(candidate, "liquidity_allocation")
        self._execution(event)
        self._drain()

    def _point(self, reference, phase, at_open=False):
        state = self._state
        stop = state.controller.stop
        if (
            stop is not None
            and state.account.net_base
            and reference <= stop
            and state.controller.phase != "stopped_holding"
        ):
            self._price(reference, phase)

        # Sells already working at this point execute before a target-touch
        # observation freezes/cancels any remaining orders.
        def priority(order):
            return (
                0 if order.intent.purpose == "flatten" else 1 if order.intent.side == "sell" else 2,
                order.accepted_sequence,
                order.intent.client_order_id,
            )

        initial = sorted(list(self._state.orders.values()), key=priority)
        for old in initial:
            order = self._state.orders[old.intent.client_order_id]
            if not eligible(self._state, order) or order.intent.side != "sell":
                continue
            if order.intent.order_type == "market":
                emergency = (
                    order.intent.purpose == "flatten"
                    and self._state.controller.terminal_reason == "stop"
                    and self._state.stop_reference_bar != self._state.bar_index
                )
                if not at_open and not emergency:
                    continue
            elif reference < order.intent.limit_price:
                continue
            self._match(order.intent.client_order_id, reference, at_open)
            if order.intent.purpose == "flatten" and not at_open:
                candidate = self._state.model_copy(deep=True)
                candidate.stop_reference_bar = candidate.bar_index
                self._persist(candidate, "stop_reference_consumed")
        self._price(reference, phase)
        for old in initial:
            order = self._state.orders[old.intent.client_order_id]
            if self._state.controller.terminal_action or self._state.reconciliation_required:
                break
            if not eligible(self._state, order) or order.intent.side != "buy":
                continue
            if (
                order.intent.order_type == "market"
                and not at_open
                or order.intent.limit_price is not None
                and reference > order.intent.limit_price
            ):
                continue
            self._match(order.intent.client_order_id, reference, at_open)
            if (
                self._state.controller.stop is not None
                and self._state.account.net_base
                and reference <= self._state.controller.stop
            ):
                self._point(reference, phase, at_open=False)
                break

    def _leg(self, start, end, phase):
        cursor = start
        descending = end < start
        while cursor != end:
            candidates = [end]
            for order in self._state.orders.values():
                if eligible(self._state, order) and order.intent.limit_price is not None:
                    price = order.intent.limit_price
                    if (
                        descending
                        and order.intent.side == "buy"
                        and end <= price < cursor
                        or not descending
                        and order.intent.side == "sell"
                        and cursor < price <= end
                    ):
                        candidates.append(price)
            stop = self._state.controller.stop
            if (
                descending
                and stop is not None
                and self._state.account.net_base
                and end <= stop < cursor
            ):
                candidates.append(stop)
            following = max(candidates) if descending else min(candidates)
            self._point(following, phase)
            cursor = following

    @exact
    def on_candle(self, candle):
        if self._failed:
            raise RuntimeError("Spot storage failed; restore a fresh session")
        if self._state.finished:
            raise ValueError("Spot paper session has reached end of data")
        if self._state.bar_in_progress:
            raise RuntimeError("incomplete candle requires fresh recovery")
        if len(candle) != 6 or isinstance(candle[0], bool):
            raise ValueError("Spot candle must have six OHLCV values")
        values = [number(x) for x in candle]
        timestamp, opening, high, low, close, volume = values
        interval = timeframe_ms(self._state.binding.timeframe)
        if (
            timestamp < 0
            or timestamp != int(timestamp)
            or int(timestamp) % interval
            or min(opening, high, low, close) <= 0
            or volume < 0
            or not low <= min(opening, close) <= max(opening, close) <= high
        ):
            raise ValueError("invalid Spot OHLCV candle")
        if self._state.last_candle is not None:
            if timestamp == self._state.last_candle[0]:
                if values == self._state.last_candle:
                    return
                raise ValueError("conflicting reconnect candle")
            if timestamp != self._state.last_candle[0] + interval:
                raise ValueError("Spot candle gap or out-of-order input")
        b = self._state.binding
        if b.evaluation_end_ms is not None and timestamp >= b.evaluation_end_ms:
            raise ValueError("candle outside evaluation interval")
        if (
            self._state.instrument.status == "historical"
            and not self._state.instrument.effective_from_ms
            <= timestamp
            <= self._state.instrument.effective_to_ms
        ):
            raise ValueError("historical instrument evidence does not cover candle")
        self._timestamp = int(timestamp)
        candidate = self._state.model_copy(deep=True)
        candidate.bar_index += 1
        candidate.bar_in_progress = True
        candidate.last_candle = values
        candidate.history = (candidate.history + [values])[-DEFAULT_HISTORY_BARS:]
        candidate.liquidity_allowance = volume * candidate.policy.maximum_volume_participation
        candidate.liquidity_used = ZERO
        levels = [
            o.intent.limit_price
            for o in candidate.orders.values()
            if eligible(candidate, o)
            and o.intent.limit_price is not None
            and low <= o.intent.limit_price <= high
        ]
        if len(levels) > 1:
            candidate.trace.append(
                {
                    "kind": "ambiguity",
                    "bar_index": candidate.bar_index,
                    "candidates": [decimal_text(p) for p in levels],
                    "policy": "open_low_high_close",
                    "selected": "chronological_crossings",
                }
            )
        self._persist(candidate, "candle_received")
        for order in list(self._state.orders.values()):
            if (
                eligible(self._state, order)
                and order.intent.expires_after_bars
                and self._state.bar_index
                >= order.intent.activation_bar + order.intent.expires_after_bars
            ):
                self._execution(status_event(self._state, order, "expired", self._timestamp))
        self._drain()
        self._point(opening, "open", True)
        self._leg(opening, low, "descending")
        self._leg(low, high, "ascending")
        self._leg(high, close, "close")
        self._price(close, "close")
        data = np.array([[float(v) for v in row] for row in self._state.history])
        s = self._strategy
        s.timestamp_ms = self._timestamp
        s.decision_timestamp_ms = self._timestamp + interval
        s.bar_index = self._state.bar_index
        s.open, s.high, s.low, s.close, s.volume = map(float, values[1:])
        s.opens, s.highs, s.lows, s.closes, s.volumes = (data[:, i] for i in range(1, 6))
        s.account_value = float(
            self._state.account.quote_cash + self._state.account.net_base * close
        )
        s.position_size = float(self._state.account.net_base)
        s.position_direction = "long" if s.position_size else None
        s.spot_runtime = self.report(mark=close)
        s.on_bar()
        request = s.get_spot_recipe_request()
        self._observe(
            "closed_bar",
            {
                "bar_index": self._state.bar_index,
                "close": decimal_text(close),
                "start": bool(
                    request
                    and request.start
                    and (b.evaluation_start_ms is None or timestamp >= b.evaluation_start_ms)
                ),
                "stop_hold": bool(request and request.stop_hold),
                "stop_flatten": bool(request and request.stop_flatten),
                "decision_timestamp_ms": int(timestamp) + interval,
            },
        )
        self._drain()
        candidate = self._state.model_copy(deep=True)
        candidate.graph_state = s.export_graph_state()
        report = spot_execution_report(candidate, mark=close)
        if report["net_equity"] is not None:
            equity = Decimal(report["net_equity"])
            candidate.peak_equity = max(candidate.peak_equity, equity)
            candidate.maximum_drawdown = max(
                candidate.maximum_drawdown, (candidate.peak_equity - equity) / candidate.peak_equity
            )
            candidate.equity_curve.append(
                {
                    "timestamp_ms": int(timestamp) + interval,
                    "net_equity": decimal_text(equity),
                    "inventory": decimal_text(candidate.account.net_base),
                }
            )
        candidate.bar_in_progress = False
        self._persist(candidate, "candle_processed")

    def command(self, command):
        self._observe("command", command)
        self._drain()

    @exact
    def finish(self, *, policy=None):
        action = policy or self._state.binding.end_of_data_policy
        if self._state.finished:
            prior = self._state.commands.get("end-of-data")
            if prior is None or prior["action"] != action:
                raise ValueError("conflicting end-of-data policy")
            return
        self.command({"command_id": "end-of-data", "action": action, "reason": "end_of_data"})
        if action == "stop_and_flatten" and not self._state.reconciliation_required:
            # One synthetic closing observation, sharing the final bar's unused
            # volume. It is never an extra candle or a refreshed allowance.
            for order in list(self._state.orders.values()):
                if (
                    order.intent.purpose == "flatten"
                    and order.status not in TERMINAL
                    and self._state.mark is not None
                ):
                    self._match(order.intent.client_order_id, self._state.mark, False)
        candidate = self._state.model_copy(deep=True)
        candidate.finished = True
        self._persist(candidate, "end_of_data")

    def report(self, *, mark):
        return spot_execution_report(self._state, mark=mark)

    def export_state(self):
        return export_spot_state(self._state)

    def restore_state(self, document, *, journal_suffix=()):
        if not self._fresh or self._failed:
            raise ValueError("restore requires a fresh paper session")
        state = restore_spot_state(document, expected_binding=self._state.binding)
        for record in journal_suffix:
            unsigned = {k: v for k, v in record.items() if k != "sha256"}
            if (
                spot_content_hash(unsigned) != record["sha256"]
                or record["sequence"] != state.journal_sequence + 1
                or record["previous_sha256"] != state.journal_sha256
            ):
                raise ValueError("incomplete or conflicting journal suffix")
            state = restore_spot_state(record["state"], expected_binding=self._state.binding)
            state.journal_sequence = record["sequence"]
            state.journal_sha256 = record["sha256"]
        if state.bar_in_progress or state.pending_observation is not None:
            raise ValueError(
                "incomplete candle checkpoint requires complete committed journal suffix"
            )
        if state.graph_state:
            self._strategy.restore_graph_state(state.graph_state)
        self._state = state
        self._timestamp = int(state.last_candle[0]) if state.last_candle else 0
        self._fresh = False
