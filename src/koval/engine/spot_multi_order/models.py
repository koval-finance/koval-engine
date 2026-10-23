"""Version-one public vocabulary for cash-funded Spot recipes; no transport."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from koval.engine.spot_numbers import InputNumber, Number, exact
from koval.strategy.schemas import SpotDcaParams, SpotGridParams

ZERO = Decimal(0)
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})
FEATURES = (
    "spot_multi_order_v1",
    "spot_reservations_v1",
    "spot_causal_ohlcv_v1",
    "spot_state_restore_v1",
)
Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
Count = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Positive = Annotated[InputNumber, Field(gt=0)]
Nonnegative = Annotated[Number, Field(ge=0)]


class Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, revalidate_instances="always"
    )


def spot_content_hash(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class SpotBindingV1(Value):
    version: Literal["koval_spot_multi_order_v1"] = "koval_spot_multi_order_v1"
    account_id: Id
    session_id: Id
    symbol: Id
    base_asset: Id
    quote_asset: Id
    exchange: Literal["binance"] = "binance"
    market: Literal["spot"] = "spot"
    mode: Literal["paper", "historical"] = "paper"
    timeframe: Id
    graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    software_identity: Id
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_quote_cash: Positive
    end_of_data_policy: Literal["stop_and_hold", "stop_and_flatten"]
    evaluation_start_ms: Count | None = None
    evaluation_end_ms: Count | None = None
    initial_peak_equity: Positive | None = None

    @model_validator(mode="after")
    def identity(self):
        if self.base_asset == self.quote_asset or self.symbol != self.base_asset + self.quote_asset:
            raise ValueError("Spot asset/symbol identity mismatch")
        from koval.exchanges.base import timeframe_ms

        timeframe_ms(self.timeframe)
        if (self.evaluation_start_ms is None) != (self.evaluation_end_ms is None):
            raise ValueError("evaluation boundaries must be supplied together")
        if (
            self.evaluation_end_ms is not None
            and self.evaluation_end_ms <= self.evaluation_start_ms
        ):
            raise ValueError("invalid evaluation boundaries")
        return self


class SpotInstrumentEvidenceV1(Value):
    version: Literal["koval_spot_instrument_v1"] = "koval_spot_instrument_v1"
    evidence_id: Id
    symbol: Id
    base_asset: Id
    quote_asset: Id
    exchange: Literal["binance"] = "binance"
    market: Literal["spot"] = "spot"
    source: str = Field(min_length=1, max_length=2048)
    status: Literal["historical", "current_snapshot", "approximation", "synthetic_fixture"]
    effective_from_ms: Count
    effective_to_ms: Count
    acquired_at_ms: Count
    price_tick: Positive
    quantity_step: Positive
    market_quantity_step: Positive
    minimum_quantity: Positive
    maximum_quantity: Positive
    minimum_notional: Positive
    maximum_notional: Positive
    minimum_price: Positive
    maximum_price: Positive
    maximum_open_orders: int = Field(strict=True, ge=1, le=1024)
    market_minimum_quantity: Positive | None = None
    market_maximum_quantity: Positive | None = None
    apply_notional_to_market: bool = True
    price_band_low: Positive | None = None
    price_band_high: Positive | None = None
    price_band_source: str | None = None
    unknown_active_filters: tuple[str, ...] = ()

    @model_validator(mode="after")
    def bounds(self):
        if self.effective_to_ms < self.effective_from_ms or self.unknown_active_filters:
            raise ValueError("invalid coverage or unsupported active filters")
        if self.base_asset + self.quote_asset != self.symbol or self.base_asset == self.quote_asset:
            raise ValueError("instrument identity mismatch")
        for low, high in (
            (self.minimum_price, self.maximum_price),
            (self.minimum_quantity, self.maximum_quantity),
            (self.minimum_notional, self.maximum_notional),
        ):
            if low > high:
                raise ValueError("inverted instrument bounds")
        if (self.price_band_low is None) != (self.price_band_high is None):
            raise ValueError("price bands must be paired")
        if self.price_band_low is not None and (
            not self.price_band_source or self.price_band_low >= self.price_band_high
        ):
            raise ValueError("invalid price-band evidence")
        return self


class SpotFillPolicyV1(Value):
    version: Literal["koval_spot_ohlcv_v1"] = "koval_spot_ohlcv_v1"
    path: Literal["open_low_high_close"] = "open_low_high_close"
    policy_id: Id
    maker_fee_bps: InputNumber = Field(lt=10000)
    taker_fee_bps: InputNumber = Field(lt=10000)
    fee_asset: Id
    fee_source: str = Field(min_length=1, max_length=2048)
    buy_quote_fee_cap_bps: InputNumber = Field(lt=10000)
    sell_base_fee_cap_bps: InputNumber = Field(lt=10000)
    spread_bps: InputNumber = Field(le=1000)
    slippage_bps: InputNumber = Field(le=1000)
    maximum_volume_participation: InputNumber = Field(gt=0, le=1)
    expires_after_bars: int | None = Field(None, strict=True, ge=1, le=1000000)
    third_asset_quote_price: Positive | None = None
    third_asset_allowance_quote: InputNumber = Decimal(0)


class SpotRecipeV1(Value):
    kind: Literal["dca", "grid"]
    params: dict

    @model_validator(mode="after")
    @exact
    def parameters(self):
        cls = SpotDcaParams if self.kind == "dca" else SpotGridParams
        # object assignment avoids recursively triggering validate_assignment.
        object.__setattr__(self, "params", cls.model_validate(self.params).model_dump(mode="json"))
        return self


class SpotOrderIntentV1(Value):
    account_id: Id
    session_id: Id
    symbol: Id
    intent_id: Id
    client_order_id: Id
    cycle_id: Id
    slot_id: Id
    generation: Count
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    purpose: Literal["base", "addition", "grid_buy", "grid_sell", "target", "flatten"]
    quantity: Nonnegative | None = None
    quote_notional_cap: Nonnegative | None = None
    limit_price: Nonnegative | None = None
    activation_bar: Count
    expires_after_bars: Count | None = None
    requested_quantity: Nonnegative | None = None
    requested_price: Nonnegative | None = None
    parent_order_id: Id | None = None
    terminal_command_id: Id | None = None

    @model_validator(mode="after")
    def shape(self):
        if (self.quantity is None) == (self.quote_notional_cap is None):
            raise ValueError("order requires quantity XOR quote_notional_cap")
        if (self.quantity or self.quote_notional_cap) <= 0:
            raise ValueError("order size must be positive")
        if self.side == "sell" and self.quantity is None:
            raise ValueError("SELL requires actual base quantity")
        if self.order_type == "limit" and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("limit order requires positive limit price")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("market order has no limit price")
        if self.side != ("buy" if self.purpose in {"base", "addition", "grid_buy"} else "sell"):
            raise ValueError("order purpose/side mismatch")
        return self


class SpotCancelIntentV1(Value):
    command_id: Id
    account_id: Id
    session_id: Id
    symbol: Id
    client_order_id: Id
    generation: Count


class SpotReplaceIntentV1(SpotCancelIntentV1):
    replacement: SpotOrderIntentV1


class SpotFeeV1(Value):
    asset: Id
    quantity: Nonnegative
    quote_value: Nonnegative | None = None
    source: str | None = None


class SpotExecutionEventV1(Value):
    event_id: Id
    account_id: Id
    session_id: Id
    symbol: Id
    client_order_id: Id
    exchange_order_id: Id | None = None
    generation: Count
    source_sequence: int = Field(strict=True, ge=1, le=2**63 - 1)
    timestamp_ms: Count
    side: Literal["buy", "sell"]
    purpose: Literal["base", "addition", "grid_buy", "grid_sell", "target", "flatten"]
    status: Literal["accepted", "partial", "filled", "canceled", "expired", "rejected", "unknown"]
    trade_id: Id | None = None
    quantity: Nonnegative = ZERO
    price: Nonnegative | None = None
    quote_notional: Nonnegative = ZERO
    cumulative_quantity: Nonnegative = ZERO
    cumulative_quote: Nonnegative = ZERO
    fees: tuple[SpotFeeV1, ...] = ()

    @model_validator(mode="after")
    @exact
    def fill_shape(self):
        if self.quantity:
            if (
                not self.trade_id
                or not self.price
                or self.quantity * self.price != self.quote_notional
                or self.status in {"accepted", "unknown", "rejected"}
            ):
                raise ValueError("invalid execution delta/notional")
        elif self.price is not None or self.quote_notional or self.fees or self.trade_id:
            raise ValueError("zero delta status must have no price/notional/fees/trade")
        if self.status not in {"rejected", "unknown"} and not self.exchange_order_id:
            raise ValueError("accepted execution requires order identity")
        return self


class SpotOrderStateV1(Value):
    intent: SpotOrderIntentV1
    status: str = "pending"
    exchange_order_id: str | None = None
    filled_quantity: Nonnegative = ZERO
    filled_quote: Nonnegative = ZERO
    reserved_quote: Nonnegative = ZERO
    reserved_base: Nonnegative = ZERO
    terminal_reconciled: bool = False
    cancel_command_id: str | None = None
    accepted_sequence: Count = 0
    allocations: dict[str, Number] = Field(default_factory=dict)
    terminal_bar: int | None = None
    unknown_outcome: Literal["submit", "cancel"] | None = None


class SpotLotV1(Value):
    lot_id: Id
    slot_id: Id
    creation_sequence: Count
    quantity: Nonnegative = ZERO
    cost: Nonnegative = ZERO
    bought_base: Nonnegative = ZERO
    bought_quote: Nonnegative = ZERO
    released_fraction: Nonnegative = ZERO


class SpotAccountStateV1(Value):
    quote_cash: Nonnegative
    allocation_cash: Nonnegative
    net_base: Nonnegative = ZERO
    remaining_cost: Nonnegative = ZERO
    realized_profit: Number = ZERO
    bought_base: Nonnegative = ZERO
    bought_quote: Nonnegative = ZERO
    cleanup_reserve: Nonnegative = ZERO
    cycle_reserve: Nonnegative = ZERO
    fees_by_asset: dict[str, Number] = Field(default_factory=dict)
    valued_third_fees: Nonnegative = ZERO
    fee_records: dict[str, dict] = Field(default_factory=dict)
    lots: dict[str, SpotLotV1] = Field(default_factory=dict)


class SpotControllerStateV1(Value):
    phase: str = "idle"
    cycle: Count = 0
    anchor: Nonnegative | None = None
    stop: Nonnegative | None = None
    desired_target: Nonnegative | None = None
    slots: dict[str, dict] = Field(default_factory=dict)
    last_cycle_bar: int = -1
    terminal_command_id: str | None = None
    terminal_action: Literal["stop_and_hold", "stop_and_flatten"] | None = None
    terminal_reason: str | None = None
    close_generation: Count = 0
    pending_replacement: dict | None = None
    refusal_reason: str | None = None
    cycle_base_start: Nonnegative = ZERO
    cycle_quote_start: Nonnegative = ZERO
    last_close_observation: Count = 0


class SpotSessionStateV1(Value):
    version: Literal["koval_spot_session_state_v1"] = "koval_spot_session_state_v1"
    binding: SpotBindingV1
    recipe: SpotRecipeV1
    instrument: SpotInstrumentEvidenceV1
    policy: SpotFillPolicyV1
    account: SpotAccountStateV1
    controller: SpotControllerStateV1 = Field(default_factory=SpotControllerStateV1)
    orders: dict[str, SpotOrderStateV1] = Field(default_factory=dict)
    events: dict[str, SpotExecutionEventV1] = Field(default_factory=dict)
    trade_ids: dict[str, str] = Field(default_factory=dict)
    event_sequence: Count = 0
    buffered_events: dict[str, SpotExecutionEventV1] = Field(default_factory=dict)
    reconciliation_required: bool = False
    reconciliation_revision: Count = 0
    quarantine: list[dict] = Field(default_factory=list)
    observations: dict[str, str] = Field(default_factory=dict)
    observation_sequence: Count = 0
    bar_index: int = -1
    last_candle: list[Number] | None = None
    liquidity_used: Nonnegative = ZERO
    liquidity_allowance: Nonnegative = ZERO
    mark: Nonnegative | None = None
    peak_equity: Nonnegative
    maximum_drawdown: Nonnegative = ZERO
    trace: list[dict] = Field(default_factory=list)
    equity_curve: list[dict] = Field(default_factory=list)
    commands: dict[str, dict] = Field(default_factory=dict)
    valuations: dict[str, dict] = Field(default_factory=dict)
    graph_state: dict = Field(default_factory=dict)
    history: list[list[Number]] = Field(default_factory=list)
    journal_sequence: Count = 0
    journal_sha256: str | None = None
    stop_reference_bar: int | None = None
    fee_reconciliation_required: bool = False
    replacements: dict[str, SpotReplaceIntentV1] = Field(default_factory=dict)
    finished: bool = False
    bar_in_progress: bool = False
    pending_observation: dict | None = None
    price_bar: int = -1
    price_phase: int = -1
    price_crossing_index: Count = 0
    crossing_digests: dict[str, str] = Field(default_factory=dict)
    bar_ids: dict[str, str] = Field(default_factory=dict)
    closed_bar_digests: dict[str, str] = Field(default_factory=dict)


class SpotPricePayloadV1(Value):
    price: Nonnegative
    bar_index: Count
    phase: Literal["open", "descending", "ascending", "close"]
    crossing_index: Count | None = None
    bar_id: Id | None = None


class SpotClosedBarPayloadV1(Value):
    bar_index: Count
    close: Nonnegative
    start: bool = Field(False, strict=True)
    stop_hold: bool = Field(False, strict=True)
    stop_flatten: bool = Field(False, strict=True)
    ohlcv: list[Number] | None = Field(None, min_length=6, max_length=6)
    decision_timestamp_ms: Count | None = None

    @model_validator(mode="after")
    def candle(self):
        if self.ohlcv is not None:
            timestamp, opening, high, low, close, volume = self.ohlcv
            if (
                timestamp < 0
                or timestamp != int(timestamp)
                or min(opening, high, low, close) <= 0
                or volume < 0
                or not low <= min(opening, close) <= max(opening, close) <= high
                or close != self.close
            ):
                raise ValueError("invalid closed candle OHLCV")
        return self


class SpotReconciliationV1(Value):
    account_id: Id
    session_id: Id
    symbol: Id
    revision: Count
    complete: bool
    source_sequence: Count
    quote_cash: Nonnegative
    net_base: Nonnegative
    orders: dict[str, dict]
    working_order_ids: tuple[str, ...]
    events: tuple[SpotExecutionEventV1, ...] = ()
    fee_valuations: tuple[dict, ...] = ()


class SpotFeeValuationV1(Value):
    valuation_id: Id
    account_id: Id
    session_id: Id
    fee_id: Id
    asset: Id
    quantity: Nonnegative
    quote_value: Nonnegative
    valuation_timestamp_ms: Count
    source: str = Field(min_length=1)
    evidence_id: Id
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SpotObservationV1(Value):
    observation_id: Id
    sequence: Count
    timestamp_ms: Count
    kind: Literal["closed_bar", "price", "executions", "command", "reconciliation", "fee_valuation"]
    payload: dict


class SpotDecisionV1(Value):
    intents: list[dict] = Field(default_factory=list)
    controller: SpotControllerStateV1 | None = None
    cycle_reserve: Nonnegative | None = None
    cleanup_reserve: Nonnegative | None = None


class SpotTransitionV1(Value):
    state: SpotSessionStateV1
    decisions: SpotDecisionV1
    records: list[dict]


class SpotAccountingViolation(ValueError):
    """An attributed execution cannot fit the authorized allocation or fee caps."""
