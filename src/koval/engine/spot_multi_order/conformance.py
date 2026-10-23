"""Public adapter acceptance runner and strict ordered Decimal comparison."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from koval.engine.spot_numbers import exact

from .account import cancel_spot_order, paper_reconciliation, replace_spot_order, submit_spot_order
from .execution import advance_spot
from .models import SpotObservationV1
from .paper import SpotPaperSessionV1
from .state import create_spot_session, export_spot_state, restore_spot_state, spot_execution_report


@dataclass(frozen=True)
class SpotConformanceResultV1:
    matches: bool
    differences: tuple[str, ...]


@exact
def compare_spot_execution_reports(expected, actual, *, absolute_tolerance="0.000000000000000001"):
    tolerance = Decimal(absolute_tolerance)
    if not tolerance.is_finite() or not 0 <= tolerance <= Decimal("1e-18"):
        raise ValueError("fixture tolerance must be finite, nonnegative and <=1e-18")
    differences = []

    def compare(left, right, path):
        if isinstance(left, dict):
            if not isinstance(right, dict):
                differences.append(f"{path}: expected object")
                return
            for key, value in left.items():
                if key not in right:
                    differences.append(f"{path}.{key}: missing")
                else:
                    compare(value, right[key], f"{path}.{key}")
        elif isinstance(left, list):
            if not isinstance(right, list) or len(left) != len(right):
                differences.append(f"{path}: ordered list length mismatch")
            else:
                for i, (a, b) in enumerate(zip(left, right, strict=True)):
                    compare(a, b, f"{path}[{i}]")
        elif isinstance(left, str) and isinstance(right, str):
            field = path.rsplit(".", 1)[-1]
            tolerant = {
                "cash",
                "quote_cash",
                "allocation_cash",
                "net_equity",
                "gross_equity",
                "realized_profit",
                "gross_average",
                "fee_adjusted_unit_cost",
                "inventory_cost",
                "marked_inventory",
                "cost",
            }
            exact_numbers = {
                "base_quantity",
                "base_reserved",
                "free_base",
                "quote_reserved",
                "order_quote_reserved",
                "cycle_quote_reserved",
                "cleanup_quote_reserved",
                "free_quote",
                "quantity",
                "price",
                "limit_price",
                "quote_notional_cap",
                "quote_notional",
                "cumulative_quantity",
                "cumulative_quote",
                "reserved_base",
                "reserved_quote",
                "gross_bought_base",
                "gross_buy_notional",
                "terminal_holding",
            }
            if field not in tolerant | exact_numbers:
                if left != right:
                    differences.append(f"{path}: identity/status mismatch {left!r} != {right!r}")
                return
            try:
                a, b = Decimal(left), Decimal(right)
            except InvalidOperation:
                if left != right:
                    differences.append(f"{path}: {left!r} != {right!r}")
            else:
                difference = abs(a - b)
                allowed = tolerance if field in tolerant else Decimal(0)
                if (
                    not a.is_finite()
                    or not b.is_finite()
                    or a >= 0 > b
                    or difference
                    and (not allowed or difference >= allowed)
                ):
                    differences.append(f"{path}: {left} != {right}")
        elif type(left) is not type(right) or left != right:
            differences.append(f"{path}: {left!r} != {right!r}")

    compare(expected, actual, "report")
    return SpotConformanceResultV1(not differences, tuple(differences))


def acceptance_snapshot(state, mark):
    report = spot_execution_report(state, mark=mark)
    report["phase"] = state.controller.phase
    report["order_states"] = [
        {
            "client_order_id": key,
            "generation": o.intent.generation,
            "status": o.status,
            "unknown_outcome": o.unknown_outcome,
            "filled_quantity": str(o.filled_quantity),
            "reserved_quote": str(o.reserved_quote),
            "reserved_base": str(o.reserved_base),
        }
        for key, o in state.orders.items()
    ]
    report["fills"] = [
        {
            "client_order_id": e.client_order_id,
            "purpose": e.purpose,
            "side": e.side,
            "quantity": str(e.quantity),
            "price": str(e.price),
            "fees": [f.model_dump(mode="json") for f in e.fees],
        }
        for e in state.events.values()
        if e.quantity
    ]
    report["intents"] = [
        {
            "client_order_id": o.intent.client_order_id,
            "purpose": o.intent.purpose,
            "slot_id": o.intent.slot_id,
            "generation": o.intent.generation,
            "quantity": None if o.intent.quantity is None else str(o.intent.quantity),
            "quote_notional_cap": None
            if o.intent.quote_notional_cap is None
            else str(o.intent.quote_notional_cap),
            "limit_price": None if o.intent.limit_price is None else str(o.intent.limit_price),
        }
        for o in state.orders.values()
    ]
    return report


def run_spot_fixture(fixture):
    if (
        fixture.get("version") != "koval_spot_fixture_v1"
        or fixture.get("ordering_policy") != "open_low_high_close"
    ):
        raise ValueError("unsupported Spot fixture version/ordering")
    paper = None
    if fixture["driver"] == "paper":
        paper = SpotPaperSessionV1(
            fixture["binding"],
            fixture["graph"],
            fixture["instrument"],
            fixture["policy"],
            on_record=lambda record: None,
        )
        state = paper.state
    elif fixture["driver"] == "controller_events":
        state = create_spot_session(
            fixture["binding"], fixture["recipe"], fixture["instrument"], fixture["policy"]
        )
    else:
        raise ValueError("unsupported fixture driver")
    differences = []
    for index, step in enumerate(fixture["observations"]):
        if paper is not None:
            if step["kind"] == "candle":
                paper.on_candle(step["candle"])
            elif step["kind"] == "command":
                paper.command(step["payload"])
            else:
                raise ValueError("unsupported paper fixture observation")
            state = paper.state
        else:
            payload = step.get("payload", {})
            kind = step["kind"]
            if kind == "simulator_reconciliation":
                kind = "reconciliation"
                payload = paper_reconciliation(state).model_dump(mode="json")
            if kind == "restore":
                state = restore_spot_state(export_spot_state(state), expected_binding=state.binding)
            else:
                try:
                    if kind == "submit_request":
                        next_state = submit_spot_order(state, payload)
                    elif kind == "cancel_request":
                        next_state = cancel_spot_order(
                            state,
                            payload["client_order_id"],
                            command_id=payload["command_id"],
                            generation=payload["generation"],
                        )
                    elif kind == "replace_request":
                        next_state = replace_spot_order(state, payload)
                    else:
                        observation = SpotObservationV1(
                            observation_id=f"fixture-{index}",
                            sequence=state.observation_sequence + 1,
                            timestamp_ms=index,
                            kind=kind,
                            payload=payload,
                        )
                        next_state = advance_spot(state, observation).state
                except ValueError as exc:
                    import re

                    if not step.get("expect_error") or not re.search(
                        step["expect_error"], str(exc)
                    ):
                        raise
                else:
                    if step.get("expect_error"):
                        differences.append(
                            f"observation[{index}]: expected refusal {step['expect_error']}"
                        )
                    state = next_state
        result = compare_spot_execution_reports(
            step["expected"], acceptance_snapshot(state, step["mark"])
        )
        differences.extend(f"observation[{index}]: {d}" for d in result.differences)
    return SpotConformanceResultV1(not differences, tuple(differences))
