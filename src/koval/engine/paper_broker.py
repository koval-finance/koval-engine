"""Simulated paper broker (MIT) - bracket / OCO fills against bar OHLC.

Two versioned profiles share this class (see ``paper_profile.py``):

``paper_legacy_v1`` (default) - the original cost-free ruleset:
- Market entry fills at the close of the signal bar (``fill_market_if_pending``).
- Limit entry (``limit`` or ``limit_at_zone``) fills on a later bar when
  ``low <= limit <= high``, at the limit price.
- Stop entry (``stop`` or ``stop_market``) fills when price trades through the
  trigger. A gap through the trigger fills at the bar open.
- Protection is active on a limit/stop entry's fill bar.
- No commission, spread, slippage or margin.

``paper_ohlcv_fixed_v1`` - the Koval execution contract v1:
- Market entry fills at the OPEN of the bar after the signal bar.
- Protection is eligible only from the bar after the entry-fill bar.
- Every fill pays ``spread_bps / 2 + slippage_bps`` in the adverse direction
  and ``commission_bps`` of notional; an entry limit is never filled worse
  than its limit price.
- Take-profit is market-on-touch (venue ``TAKE_PROFIT_MARKET``): it pays the
  full adverse adjustment and is never capped at the target.
- An entry debits ``notional / leverage`` of margin and is rejected when
  ``margin + commission`` exceeds ``equity - margin_used``.

``paper_ohlcv_realistic_v2`` preserves v1 cost arithmetic and adds favorable
limit-gap pricing, same-entry-bar protection, explicit stop-first ambiguity
records, and an equal-timestamp ordering contract. Optional evidence inputs add
funding, fee provenance, instrument normalization, mark-price liquidation, and
a partial-fill/latency OHLCV proxy without changing either v1 profile.

Shared by all profiles:
- Stop-loss fills when ``low <= SL`` (long) / ``high >= SL`` (short); a bar
  that gaps through the stop fills at the bar open.
- Take-profit fills when ``high >= TP`` (long) / ``low <= TP`` (short); a bar
  that opens beyond the target uses the (favourable) open as the reference.
- Because OHLC does not reveal intrabar ordering, a bar crossing both SL and
  TP is resolved SL-first.
- No order-book or queue-position model. Results are a deterministic simulation,
  not a prediction or bound on venue execution.
Equity = realized balance + unrealized PnL of the open position marked to the
last seen close.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from decimal import Decimal

from koval.engine.account_ledger import AccountLedger, LedgerReconciliation
from koval.engine.broker import (
    BrokerFill,
    BrokerOrderAck,
    BrokerOrderIntent,
    BrokerPositionSnapshot,
    BrokerReconciliationReport,
    ProtectiveOrderIntent,
)
from koval.engine.execution_evidence import ExecutionEvidence, ExecutionEvidenceUpdate
from koval.engine.execution_proxy import (
    BarLiquidityBudget,
    ExecutionProxyConfig,
    ExecutionTimeline,
    SlippageResolution,
    execution_timeline,
    resolve_slippage_bps,
)
from koval.engine.fee_evidence import (
    FeeApplication,
    FeeScheduleEvidence,
    resolve_fee_application,
)
from koval.engine.funding import FundingSeries, funding_cashflow
from koval.engine.instrument_risk import (
    InstrumentSpecEvidence,
    MarkPriceRecord,
    MarkPriceSeries,
    evaluate_liquidation,
    normalize_order,
    select_instrument_spec,
    validate_initial_leverage,
)
from koval.engine.paper_fills import (
    adjusted_price,
    commission,
    cost_split,
    max_quantity_for_stop_risk,
    required_margin,
)
from koval.engine.paper_profile import PAPER_LEGACY_VERSION, PaperExecutionProfile
from koval.engine.protection import validate_protection_update
from koval.engine.run_identity import content_sha256, execution_evidence_manifest
from koval.exchanges.execution_compatibility import assert_supported_market
from koval.exchanges.markets import canonical_market

_MARKET_ORDER_TYPES = frozenset({"market"})
_LIMIT_ORDER_TYPES = frozenset({"limit", "limit_at_zone"})
_STOP_ORDER_TYPES = frozenset({"stop", "stop_market"})
MAX_OBSERVED_QUOTE_AGE_MS = 5_000
OBSERVED_QUOTE_EXECUTION_MODEL = "observed_public_book_quote_v1"


@dataclass(frozen=True)
class Fill:
    kind: str  # "entry" | "stop_loss" | "take_profit" | "manual_close" | "liquidation"
    side: str  # position side: "buy" | "sell"
    price: float
    quantity: float
    timestamp_ms: int
    realized_pnl: float  # cash effect of this fill: -commission on entry
    commission: float = 0.0
    reference_price: float | None = None
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    margin: float = 0.0  # initial margin debited for an entry fill; 0 otherwise
    liquidity_role: str | None = None
    fee_rate_bps: float | None = None
    fee_currency: str | None = None
    fee_evidence_id: str | None = None
    fee_evidence_status: str | None = None
    discount_treatment: str | None = None
    fee_tier_id: str | None = None
    instrument_evidence_id: str | None = None
    liquidation_fee: float = 0.0
    status: str = "filled"
    cumulative_quantity: float | None = None
    order_id: str = ""
    execution_model: str | None = None
    impact_evidence_id: str | None = None
    decision_timestamp_ms: int | None = None
    submission_timestamp_ms: int | None = None
    acknowledgement_timestamp_ms: int | None = None
    protection_active_timestamp_ms: int | None = None


@dataclass(frozen=True)
class BrokerPosition:
    side: str
    entry_price: float
    quantity: float
    stop_price: float
    target_price: float
    entry_timestamp_ms: int
    session_id: str = ""
    entry_client_order_id: str = ""
    symbol: str = ""
    stop_client_order_id: str = ""
    target_client_order_id: str = ""
    entry_commission: float = 0.0
    margin: float = 0.0
    protection_active_timestamp_ms: int | None = None


class PaperOrderRejected(ValueError):
    """A deterministic order refusal that leaves the session available."""

    def __init__(self, detail: str, *, reason: str = "insufficient_margin") -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass
class _PendingOrder:
    side: str
    entry_price: float
    stop_price: float
    target_price: float
    quantity: float
    order_type: str
    session_id: str = ""
    client_order_id: str = ""
    symbol: str = ""
    risk_budget: float | None = None
    requested_quantity: float = 0.0
    cumulative_quantity: float = 0.0
    timeline: ExecutionTimeline | None = None


class PaperBroker:
    target = "paper"

    def __init__(
        self,
        starting_balance: float,
        *,
        profile: PaperExecutionProfile | None = None,
        market: str = "future",
        funding: FundingSeries | None = None,
        fee_schedule: FeeScheduleEvidence | None = None,
        instrument_specs: tuple[InstrumentSpecEvidence, ...] = (),
        mark_prices: MarkPriceSeries | None = None,
        execution_proxy: ExecutionProxyConfig | None = None,
        exchange: str | None = None,
        observed_quote_execution: bool = False,
    ) -> None:
        try:
            balance = float(starting_balance)
        except (TypeError, ValueError) as exc:
            raise ValueError("paper starting balance must be positive and finite") from exc
        if not math.isfinite(balance) or balance <= 0:
            raise ValueError("paper starting balance must be positive and finite")
        self._journaled_ledger_sequence = 0
        self._ledger = AccountLedger(balance)
        self._position: BrokerPosition | None = None
        self._pending: _PendingOrder | None = None
        self._last_close = 0.0
        self._last_timestamp_ms = 0
        self._last_bar_timestamp_ms: int | None = None
        self._fill_events: list[BrokerFill] = []
        self._deferred_exit: tuple[Fill, BrokerPosition, str] | None = None
        self._profile = profile or PaperExecutionProfile(PAPER_LEGACY_VERSION)
        self._market = canonical_market(market)
        self._exchange = None if exchange is None else assert_supported_market(exchange, market)[0]
        if self._market == "spot" and self._profile.leverage != 1:
            raise ValueError("spot leverage must be exactly one")
        self._funding = funding
        self._fee_schedule = fee_schedule
        if fee_schedule is not None and not self._profile.is_costed:
            raise ValueError("fee evidence requires a costed paper profile")
        self._instrument_specs = tuple(instrument_specs)
        self._mark_prices = mark_prices
        if fee_schedule is not None and fee_schedule.market not in {None, self._market}:
            raise ValueError("fee evidence market does not match paper market")
        if funding is not None and funding.market != self._market:
            raise ValueError("funding evidence market does not match paper market")
        if funding is not None and not funding.coverage_complete:
            raise ValueError("paper broker requires complete funding evidence")
        if mark_prices is not None and not mark_prices.coverage_complete:
            raise ValueError("paper broker requires complete mark-price evidence")
        if any(spec.market != self._market for spec in self._instrument_specs):
            raise ValueError("instrument evidence market does not match paper market")
        if any(Decimal(str(spec.contract_size)) != 1 for spec in self._instrument_specs):
            raise ValueError("paper base-quantity accounting requires contract_size=1")
        evidence_exchanges = {
            value.strip().lower()
            for value in (
                *(spec.exchange for spec in self._instrument_specs),
                None if fee_schedule is None else fee_schedule.exchange,
                None if funding is None else funding.exchange,
                None if mark_prices is None else mark_prices.exchange,
            )
            if value is not None
        }
        if len(evidence_exchanges) > 1:
            raise ValueError("paper execution evidence exchange mismatch")
        if self._exchange is not None and evidence_exchanges - {self._exchange}:
            raise ValueError("paper execution evidence exchange mismatch")
        self._evidence_exchanges = frozenset(evidence_exchanges)
        self._evidence_symbols = {
            self._canonical_symbol(value)
            for value in (
                *(spec.canonical_symbol for spec in self._instrument_specs),
                None if fee_schedule is None else fee_schedule.canonical_symbol,
                None if funding is None else funding.canonical_symbol,
                None if mark_prices is None else mark_prices.canonical_symbol,
            )
            if value is not None
        }
        if len(self._evidence_symbols) > 1:
            raise ValueError("paper execution evidence symbol mismatch")
        for evidence_symbol in self._evidence_symbols:
            self._validate_fee_currency(evidence_symbol)
        if execution_proxy is not None and self._profile.ambiguity_policy is None:
            raise ValueError("partial execution proxy requires paper_ohlcv_realistic_v2")
        if execution_proxy is not None and (
            execution_proxy.latency.cancellation_ms or execution_proxy.latency.replacement_ms
        ):
            raise ValueError("paper cancellation and replacement latency are not supported")
        self._execution_proxy = execution_proxy
        if observed_quote_execution and not self._profile.is_costed:
            raise ValueError("observed quote paper execution requires a costed profile")
        if observed_quote_execution and self._profile.leverage != 1.0:
            raise ValueError("observed quote paper execution requires 1x leverage")
        if (
            observed_quote_execution
            and execution_proxy is not None
            and any(vars(execution_proxy.latency).values())
        ):
            raise ValueError("observed quote paper execution requires zero proxy latency")
        self._observed_quote_execution = bool(observed_quote_execution)
        self._last_observed_quote_ms: int | None = None
        self._quote_match_active = False
        self._exit_cumulative_quantity = 0.0
        self._last_funding_timestamp_ms: int | None = None
        self._funding_cursor = 0
        self._entry_bar_ts: int | None = None
        self._entry_rejection: str | None = None
        self._ambiguities: list[dict[str, object]] = []
        self._live_evidence_update_hashes: dict[str, str] = {}
        self._live_mark_prices: dict[int, MarkPriceRecord] = {}
        self._live_funding_windows: list[FundingSeries] = []
        self._live_evidence_enabled = False

    @property
    def balance(self) -> float:
        return self._ledger.balance

    @property
    def ledger(self) -> AccountLedger:
        return self._ledger

    @property
    def profile(self) -> PaperExecutionProfile:
        return self._profile

    @property
    def observed_quote_execution(self) -> bool:
        return self._observed_quote_execution

    @property
    def last_observed_quote_ms(self) -> int | None:
        return self._last_observed_quote_ms

    @property
    def market(self) -> str:
        return self._market

    @property
    def exchange(self) -> str | None:
        return self._exchange

    @property
    def execution_evidence(self) -> dict[str, dict]:
        evidence = execution_evidence_manifest(
            funding=self._funding,
            fee_schedule=self._fee_schedule,
            instrument_specs=self._instrument_specs,
            mark_prices=self._mark_prices,
            execution_proxy=self._execution_proxy,
        )
        if self._live_mark_prices:
            evidence["mark_prices"] = {
                "status": "supplied",
                "sha256": content_sha256(
                    tuple(self._live_mark_prices[key] for key in sorted(self._live_mark_prices))
                ),
            }
        if self._live_funding_windows:
            evidence["funding"] = {
                "status": "supplied",
                "sha256": content_sha256(tuple(self._live_funding_windows)),
            }
        return evidence

    def validate_execution_grid(self, interval_ms: int) -> None:
        if self._funding is not None:
            self._funding.validate_execution_grid(interval_ms)

    def checkpoint(self) -> dict:
        """Return a hash-verified broker snapshot at the current journal boundary.

        The caller must replay archived execution-evidence updates into a fresh
        broker before restoration. Their content identity is checked here; no
        missing market or risk evidence is reconstructed from mutable sources.
        """
        deferred = None
        if self._deferred_exit is not None:
            fill, position, session_id = self._deferred_exit
            deferred = {
                "fill": asdict(fill),
                "position": asdict(position),
                "session_id": session_id,
            }
        value = {
            "version": "koval_paper_broker_checkpoint_v1",
            "market": self._market,
            "exchange": self._exchange,
            "profile": self._profile.as_config(),
            "execution_evidence": self.execution_evidence,
            "ledger": self._ledger.checkpoint(),
            "journaled_ledger_sequence": self._journaled_ledger_sequence,
            "position": None if self._position is None else asdict(self._position),
            "pending": None if self._pending is None else asdict(self._pending),
            "last_close": self._last_close,
            "last_timestamp_ms": self._last_timestamp_ms,
            "fill_events": [asdict(fill) for fill in self._fill_events],
            "deferred_exit": deferred,
            "exit_cumulative_quantity": self._exit_cumulative_quantity,
            "last_funding_timestamp_ms": self._last_funding_timestamp_ms,
            "funding_cursor": self._funding_cursor,
            "entry_bar_ts": self._entry_bar_ts,
            "entry_rejection": self._entry_rejection,
            "ambiguities": [dict(item) for item in self._ambiguities],
            "live_evidence_update_hashes": [
                [update_id, digest]
                for update_id, digest in sorted(self._live_evidence_update_hashes.items())
            ],
            "live_evidence_enabled": self._live_evidence_enabled,
        }
        if self._observed_quote_execution:
            value["observed_quote_execution"] = True
            value["last_observed_quote_ms"] = self._last_observed_quote_ms
            value["last_bar_timestamp_ms"] = self._last_bar_timestamp_ms
        return {**value, "sha256": content_sha256(value)}

    def restore_checkpoint(self, checkpoint: dict) -> None:
        """Replace mutable paper state from one complete, matching checkpoint."""
        value = {key: item for key, item in checkpoint.items() if key != "sha256"}
        if checkpoint.get("sha256") != content_sha256(value):
            raise ValueError("paper broker checkpoint hash mismatch")
        if value.get("version") != "koval_paper_broker_checkpoint_v1":
            raise ValueError("unsupported paper broker checkpoint version")
        if value.get("market") != self._market or value.get("exchange") != self._exchange:
            raise ValueError("paper broker checkpoint market identity mismatch")
        if value.get("profile") != self._profile.as_config():
            raise ValueError("paper broker checkpoint profile mismatch")
        if bool(value.get("observed_quote_execution")) != self._observed_quote_execution:
            raise ValueError("paper broker checkpoint execution mode mismatch")
        if value.get("execution_evidence") != self.execution_evidence:
            raise ValueError("paper broker checkpoint execution evidence mismatch")

        pending = value.get("pending")
        if pending is not None:
            pending = dict(pending)
            timeline = pending.get("timeline")
            if timeline is not None:
                pending["timeline"] = ExecutionTimeline(**timeline)
            pending = _PendingOrder(**pending)
        deferred = value.get("deferred_exit")
        restored_deferred = None
        if deferred is not None:
            restored_deferred = (
                Fill(**deferred["fill"]),
                BrokerPosition(**deferred["position"]),
                deferred["session_id"],
            )

        self._ledger = AccountLedger.from_checkpoint(value["ledger"])
        self._journaled_ledger_sequence = int(value["journaled_ledger_sequence"])
        self._position = (
            None if value.get("position") is None else BrokerPosition(**value["position"])
        )
        self._pending = pending
        self._last_close = float(value["last_close"])
        self._last_timestamp_ms = int(value["last_timestamp_ms"])
        self._fill_events = [BrokerFill(**item) for item in value.get("fill_events", ())]
        self._deferred_exit = restored_deferred
        self._exit_cumulative_quantity = float(value["exit_cumulative_quantity"])
        self._last_funding_timestamp_ms = value.get("last_funding_timestamp_ms")
        self._funding_cursor = int(value["funding_cursor"])
        self._entry_bar_ts = value.get("entry_bar_ts")
        self._entry_rejection = value.get("entry_rejection")
        self._ambiguities = [dict(item) for item in value.get("ambiguities", ())]
        self._live_evidence_update_hashes = {
            str(update_id): str(digest)
            for update_id, digest in value.get("live_evidence_update_hashes", ())
        }
        self._live_evidence_enabled = bool(value.get("live_evidence_enabled"))
        self._last_observed_quote_ms = value.get("last_observed_quote_ms")
        self._last_bar_timestamp_ms = value.get("last_bar_timestamp_ms")
        if self.checkpoint() != checkpoint:
            raise ValueError("paper broker checkpoint is not canonical")

    def validate_execution_evidence_update(self, update: ExecutionEvidenceUpdate) -> bool:
        """Validate one pre-bar live update; return False for an applied id."""
        if not isinstance(update, ExecutionEvidenceUpdate):
            raise ValueError("paper execution evidence update has an invalid type")
        digest = content_sha256(update.as_config())
        previous = self._live_evidence_update_hashes.get(update.update_id)
        if previous is not None:
            if previous != digest:
                raise ValueError("paper execution evidence retry changed content")
            return False
        last_evidence_bar = (
            self._last_bar_timestamp_ms
            if self._observed_quote_execution
            else self._last_timestamp_ms
        )
        if (
            last_evidence_bar is not None
            and last_evidence_bar > 0
            and update.timestamp_ms <= last_evidence_bar
        ):
            raise ValueError("paper execution evidence update is stale")
        if update.funding is not None:
            if self._market != "future":
                raise ValueError("funding update requires a futures paper session")
            if update.funding.market != self._market:
                raise ValueError("paper funding update market mismatch")
            if self._exchange is not None and update.funding.exchange != self._exchange:
                raise ValueError("paper funding update exchange mismatch")
            symbol = self._canonical_symbol(update.funding.canonical_symbol)
            if self._evidence_symbols and symbol not in self._evidence_symbols:
                raise ValueError("paper funding update symbol mismatch")
        spec = update.instrument_spec
        if spec is not None:
            if spec.market != self._market:
                raise ValueError("paper instrument update market mismatch")
            if self._exchange is not None and spec.exchange != self._exchange:
                raise ValueError("paper instrument update exchange mismatch")
            if self._evidence_symbols and spec.canonical_symbol not in self._evidence_symbols:
                raise ValueError("paper instrument update symbol mismatch")
        fee = update.fee_schedule
        if fee is not None:
            if fee.market not in {None, self._market}:
                raise ValueError("paper fee update market mismatch")
            if self._exchange is not None and fee.exchange not in {None, self._exchange}:
                raise ValueError("paper fee update exchange mismatch")
            if (
                fee.canonical_symbol is not None
                and self._evidence_symbols
                and fee.canonical_symbol not in self._evidence_symbols
            ):
                raise ValueError("paper fee update symbol mismatch")
        mark = update.mark_price
        if mark is not None:
            existing = self._live_mark_prices.get(mark.timestamp_ms)
            if existing is not None and existing != mark:
                raise ValueError("conflicting paper mark-price update")
        return True

    def apply_execution_evidence_update(self, update: ExecutionEvidenceUpdate) -> bool:
        """Apply one validated update before its bar, exactly once by id."""
        if not self.validate_execution_evidence_update(update):
            return False
        if update.instrument_spec is not None:
            self._apply_instrument_update(update.instrument_spec)
        if update.fee_schedule is not None:
            self._fee_schedule = update.fee_schedule
        if update.mark_price is not None:
            self._live_mark_prices[update.mark_price.timestamp_ms] = update.mark_price
        if update.funding is not None:
            self._live_funding_windows.append(update.funding)
            for record in update.funding.records:
                self._apply_funding_record(record)
        self._live_evidence_enabled = True
        self._live_evidence_update_hashes[update.update_id] = content_sha256(update.as_config())
        return True

    def _apply_instrument_update(self, spec: InstrumentSpecEvidence) -> None:
        if any(item.evidence_id == spec.evidence_id for item in self._instrument_specs):
            return
        closed: list[InstrumentSpecEvidence] = []
        for item in self._instrument_specs:
            if item.effective_to_ms is None and item.effective_from_ms < spec.effective_from_ms:
                item = replace(item, effective_to_ms=spec.effective_from_ms - 1)
            closed.append(item)
        closed.append(spec)
        self._instrument_specs = tuple(
            sorted(closed, key=lambda item: (item.effective_from_ms, item.evidence_id))
        )

    def validate_market_context(self, *, exchange: str | None, symbol: str) -> None:
        if exchange is not None:
            venue, _ = assert_supported_market(exchange, self._market)
            if self._evidence_exchanges - {venue} or self._exchange not in {None, venue}:
                raise ValueError("paper execution evidence exchange mismatch")
        if self._evidence_symbols and self._canonical_symbol(symbol) not in self._evidence_symbols:
            raise ValueError("paper execution evidence symbol mismatch")
        self._validate_fee_currency(symbol)

    def _validate_fee_currency(self, symbol: str) -> None:
        schedule = self._fee_schedule
        if not symbol:
            return
        canonical = self._canonical_symbol(symbol)
        quote = next(
            (
                currency
                for currency in (
                    "FDUSD",
                    "USDT",
                    "USDC",
                    "TUSD",
                    "BUSD",
                    "BTC",
                    "ETH",
                    "EUR",
                    "USD",
                )
                if canonical.endswith(currency)
            ),
            None,
        )
        if (
            self._exchange == "whitebit" or "whitebit" in self._evidence_exchanges
        ) and canonical.endswith("PERP"):
            quote = "USDT"
        if any(spec.collateral_currency != quote for spec in self._instrument_specs):
            raise ValueError("paper linear accounting requires quote collateral")
        if schedule is not None and schedule.currency not in {"quote", quote}:
            raise ValueError("paper fee currency must match the instrument quote currency")

    @property
    def resolved_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            **self._profile.as_config(),
            "ambiguities": list(self._ambiguities),
            "funding_status": (
                "historical"
                if self._funding is not None
                else "live_observed"
                if self._live_funding_windows
                else "unavailable"
            ),
            "execution_evidence": self.execution_evidence,
        }
        if self._profile.is_costed:
            schedule = self._fee_schedule
            metadata["fee_evidence"] = (
                {
                    "evidence_id": schedule.evidence_id,
                    "maker_bps": schedule.maker_bps,
                    "taker_bps": schedule.taker_bps,
                    "currency": schedule.currency,
                    "evidence_status": schedule.evidence_status,
                    "source": schedule.source,
                    "effective_from_ms": schedule.effective_from_ms,
                    "effective_to_ms": schedule.effective_to_ms,
                    "discount_treatment": schedule.discount_treatment,
                    "tier_id": schedule.tier_id,
                }
                if schedule is not None
                else {
                    "evidence_id": "configured-paper-profile",
                    "maker_bps": self._profile.commission_bps,
                    "taker_bps": self._profile.commission_bps,
                    "currency": "quote",
                    "evidence_status": "approximation",
                    "source": "paper_profile",
                    "discount_treatment": "not_modelled",
                    "tier_id": "configured",
                }
            )
        else:
            metadata["fee_evidence"] = {"evidence_status": "unavailable"}
        metadata["instrument_evidence"] = [
            {
                "evidence_id": spec.evidence_id,
                "effective_from_ms": spec.effective_from_ms,
                "effective_to_ms": spec.effective_to_ms,
                "evidence_status": spec.evidence_status,
                "source": spec.source,
                "margin_mode": spec.margin_mode,
                "collateral_currency": spec.collateral_currency,
                "contract_size": str(spec.contract_size),
            }
            for spec in self._instrument_specs
        ]
        metadata["mark_price_status"] = (
            "historical"
            if self._mark_prices is not None
            else "live_observed"
            if self._live_mark_prices
            else "unavailable"
        )
        proxy = self._execution_proxy
        metadata["execution_model"] = (
            proxy.model_label if proxy is not None else "fixed_ohlcv_proxy"
        )
        metadata["partial_fill_policy"] = (
            {
                "maximum_volume_participation": str(proxy.maximum_volume_participation),
                "entry_remainder": proxy.entry_remainder_policy,
                "partial_oco": "resize_both_siblings_until_flat",
                "shared_bar_volume_budget": True,
            }
            if proxy is not None
            else {"status": "unavailable"}
        )
        if self._observed_quote_execution:
            metadata["execution_model"] = OBSERVED_QUOTE_EXECUTION_MODEL
            metadata["partial_fill_policy"] = {
                "status": "not_applied",
                "reason": "OHLCV volume cannot prove public book liquidity",
            }
            metadata["spread_source"] = "observed_bid_ask"
            metadata["liquidation_model"] = "unavailable_1x_only"
        if self._profile.ambiguity_policy is not None:
            report = ExecutionEvidence(
                funding=self._funding,
                fee_schedule=self._fee_schedule,
                instrument_specs=self._instrument_specs,
                mark_prices=self._mark_prices,
                execution_proxy=proxy,
            ).realism_report()
            if self._live_funding_windows:
                report["effects"]["funding"] = "live_observed_evidence"
            if self._live_mark_prices and self._instrument_specs:
                report["effects"]["liquidation"] = "live_sampled_mark_model"
            metadata["realism_report"] = report
        if proxy is not None:
            metadata["latency_ms"] = vars(proxy.latency).copy()
        return metadata

    @property
    def margin_used(self) -> float:
        return self._position.margin if self._position is not None else 0.0

    @property
    def position(self) -> BrokerPosition | None:
        return self._position

    @property
    def pending(self) -> bool:
        return self._pending is not None

    @property
    def equity(self) -> float:
        return self.balance + self._unrealized(self._last_close)

    def reconcile_ledger(self) -> LedgerReconciliation:
        return self._ledger.reconcile(
            unrealized_pnl=self._unrealized(self._last_close),
            equity=self.equity,
            margin_used=self.margin_used,
        )

    def _unrealized(self, price: float) -> float:
        p = self._position
        if p is None:
            return 0.0
        sign = 1.0 if p.side == "buy" else -1.0
        return (price - p.entry_price) * p.quantity * sign

    def _fee_rate(self, liquidity_role: str) -> float:
        if self._fee_schedule is None:
            return self._profile.commission_bps
        return (
            self._fee_schedule.maker_bps
            if liquidity_role == "maker"
            else self._fee_schedule.taker_bps
        )

    def _resolve_fee(self, liquidity_role: str, timestamp_ms: int) -> FeeApplication:
        schedule = self._fee_schedule
        if schedule is None:
            schedule = FeeScheduleEvidence(
                evidence_id="configured-paper-profile",
                maker_bps=self._profile.commission_bps,
                taker_bps=self._profile.commission_bps,
                currency="quote",
                evidence_status="approximation",
                source="paper_profile",
                discount_treatment="not_modelled",
                tier_id="configured",
            )
        return resolve_fee_application(
            schedule,
            role=liquidity_role,
            timestamp_ms=int(timestamp_ms),
        )

    @staticmethod
    def _fee_metadata(application: FeeApplication) -> dict[str, object]:
        return {
            "liquidity_role": application.liquidity_role,
            "rate_bps": application.rate_bps,
            "evidence_id": application.evidence_id,
            "evidence_status": application.evidence_status,
            "source": application.source,
            "discount_treatment": application.discount_treatment,
            "tier_id": application.tier_id,
        }

    def _affordability_error(
        self,
        quantity: float,
        price: float,
        *,
        liquidity_role: str = "taker",
        margin_mark: float | None = None,
    ) -> str | None:
        """Reason string when the fixed profile cannot afford an entry that
        would fill at ``price``, or ``None`` when it can. Used both as a
        nominal pre-flight in ``submit_bracket`` and again against the real
        adverse-adjusted fill price in ``process_bar``."""
        if not self._profile.is_costed:
            return None
        needed = required_margin(quantity, price, leverage=self._profile.leverage)
        fee = commission(
            quantity,
            price,
            commission_bps=self._fee_rate(liquidity_role),
        )
        available = (
            self.equity if margin_mark is None else self.balance + self._unrealized(margin_mark)
        ) - self.margin_used
        if needed + fee > available:
            return f"insufficient_margin: required {needed + fee:.8f}, available {available:.8f}"
        return None

    def _reject_unaffordable_entry(
        self, quantity: float, fill_price: float, *, liquidity_role: str, margin_mark: float
    ) -> bool:
        """Drop the pending entry and record the reason when it cannot be
        afforded at ``fill_price``. Returns ``True`` when it rejected."""
        reason = self._affordability_error(
            quantity, fill_price, liquidity_role=liquidity_role, margin_mark=margin_mark
        )
        if reason is None:
            return False
        self._pending = None
        self._entry_rejection = reason
        return True

    def consume_entry_rejection(self) -> str | None:
        """Return and clear a fill-time entry rejection recorded by ``process_bar``.

        A fixed-profile entry that clears the nominal affordability check at
        submission can still be unaffordable once the adverse spread/slippage
        adjustment is applied to the fill price. ``process_bar`` drops that
        order and records the reason here so the engine can emit
        ``ORDER_REJECTED`` and keep the session running."""
        reason, self._entry_rejection = self._entry_rejection, None
        return reason

    def submit_bracket(
        self,
        *,
        side: str,
        entry_price: float,
        stop_price: float,
        target_price: float,
        quantity: float,
        order_type: str,
        session_id: str = "",
        client_order_id: str = "",
        symbol: str = "",
        risk_budget: float | None = None,
        decision_timestamp_ms: int | None = None,
    ) -> None:
        entry_price = float(entry_price)
        stop_price = float(stop_price)
        target_price = float(target_price)
        quantity = float(quantity)
        self._validate_fee_currency(symbol)
        if symbol and self._evidence_symbols:
            if self._canonical_symbol(symbol) not in self._evidence_symbols:
                raise ValueError("paper execution evidence symbol mismatch")
        if side not in {"buy", "sell"}:
            raise ValueError("invalid paper order: side must be 'buy' or 'sell'")
        if self._market == "spot" and side == "sell":
            raise PaperOrderRejected(
                "spot paper execution does not support short entries",
                reason="spot_short_unsupported",
            )
        if order_type not in _MARKET_ORDER_TYPES | _LIMIT_ORDER_TYPES | _STOP_ORDER_TYPES:
            raise ValueError("invalid paper order: unsupported order type")
        values = (entry_price, stop_price, target_price, quantity)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("invalid paper order: prices and quantity must be positive and finite")
        if side == "buy" and not stop_price < entry_price < target_price:
            raise ValueError(
                "invalid paper order: long protection must satisfy stop < entry < target"
            )
        if side == "sell" and not target_price < entry_price < stop_price:
            raise ValueError(
                "invalid paper order: short protection must satisfy target < entry < stop"
            )
        if risk_budget is not None and (
            not math.isfinite(float(risk_budget)) or float(risk_budget) <= 0
        ):
            raise ValueError("invalid paper order: risk_budget must be positive and finite")
        liquidity_role = "maker" if order_type in _LIMIT_ORDER_TYPES else "taker"
        reason = self._affordability_error(quantity, entry_price, liquidity_role=liquidity_role)
        if reason is not None:
            raise PaperOrderRejected(reason)
        self._pending = _PendingOrder(
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            quantity=quantity,
            order_type=order_type,
            session_id=session_id,
            client_order_id=client_order_id,
            symbol=symbol,
            risk_budget=None if risk_budget is None else float(risk_budget),
            requested_quantity=quantity,
            timeline=(
                execution_timeline(int(decision_timestamp_ms), self._execution_proxy.latency)
                if decision_timestamp_ms is not None and self._execution_proxy is not None
                else None
            ),
        )

    @staticmethod
    def _canonical_symbol(symbol: str) -> str:
        return str(symbol).replace("/", "").replace("_", "").upper()

    def submit_entry(self, intent: BrokerOrderIntent) -> BrokerOrderAck:
        try:
            self.submit_bracket(
                side=intent.side,
                entry_price=float(intent.price or 0.0),
                stop_price=float(intent.stop_price or 0.0),
                target_price=float(intent.target_price or 0.0),
                quantity=float(intent.quantity),
                order_type=intent.order_type,
                session_id=intent.session_id,
                client_order_id=intent.client_order_id,
                symbol=intent.symbol,
                risk_budget=_optional_positive_float(intent.metadata.get("risk_budget")),
                decision_timestamp_ms=_optional_timestamp_ms(
                    intent.metadata.get("decision_timestamp_ms")
                ),
            )
        except PaperOrderRejected as exc:
            return BrokerOrderAck(
                session_id=intent.session_id,
                client_order_id=intent.client_order_id,
                exchange_order_id=None,
                status="rejected",
                target=self.target,
                metadata={"paper": True, "reason": exc.reason, "detail": str(exc)},
            )
        return BrokerOrderAck(
            session_id=intent.session_id,
            client_order_id=intent.client_order_id,
            exchange_order_id=None,
            status="accepted",
            target=self.target,
            metadata={"paper": True},
        )

    def fill_market_if_pending(self, *, ts_ms: int, price: float) -> Fill | None:
        # The fixed profile fills market entries at the open of the following
        # bar, inside process_bar; there is nothing to fill at the signal close.
        o = self._pending
        if o is None or o.order_type not in _MARKET_ORDER_TYPES or self._profile.is_costed:
            return None
        return self._open(
            o,
            reference=float(price),
            fill_price=float(price),
            ts_ms=ts_ms,
            liquidity_role="taker",
        )

    def _open(
        self,
        o: _PendingOrder,
        *,
        reference: float,
        fill_price: float,
        ts_ms: int,
        liquidity_role: str,
        fill_quantity: float | None = None,
        slippage: SlippageResolution | None = None,
    ) -> Fill:
        quantity = o.quantity if fill_quantity is None else float(fill_quantity)
        fee = 0.0
        spread = slip = 0.0
        margin = 0.0
        fee_application: FeeApplication | None = None
        instrument_evidence_id = (
            select_instrument_spec(self._instrument_specs, timestamp_ms=int(ts_ms)).evidence_id
            if self._instrument_specs
            else None
        )
        if self._profile.is_costed:
            fee_application = self._resolve_fee(liquidity_role, ts_ms)
            fee = commission(
                quantity,
                fill_price,
                commission_bps=fee_application.rate_bps,
            )
            effective_slippage_bps = (
                self._profile.slippage_bps if slippage is None else float(slippage.slippage_bps)
            )
            spread, slip = cost_split(
                reference,
                fill_price,
                quantity,
                spread_bps=0.0 if self._quote_match_active else self._profile.spread_bps,
                slippage_bps=effective_slippage_bps,
            )
            margin = required_margin(quantity, fill_price, leverage=self._profile.leverage)
            self._ledger.record(
                timestamp_ms=int(ts_ms),
                kind="commission",
                amount=-fee,
                reference_id=o.client_order_id,
                currency=fee_application.currency,
                metadata=self._fee_metadata(fee_application),
            )
        prior = self._position
        if o.timeline is not None:
            activation = (
                prior.protection_active_timestamp_ms
                if prior is not None
                else int(ts_ms) + self._execution_proxy.latency.protection_activation_ms
            )
            o.timeline = replace(o.timeline, protection_active_timestamp_ms=activation)
        if prior is None:
            self._position = BrokerPosition(
                side=o.side,
                entry_price=fill_price,
                quantity=quantity,
                stop_price=o.stop_price,
                target_price=o.target_price,
                entry_timestamp_ms=int(ts_ms),
                session_id=o.session_id,
                entry_client_order_id=o.client_order_id,
                symbol=o.symbol,
                entry_commission=fee,
                margin=margin,
                protection_active_timestamp_ms=(
                    None if o.timeline is None else o.timeline.protection_active_timestamp_ms
                ),
            )
        else:
            total_quantity = prior.quantity + quantity
            average_price = (
                prior.entry_price * prior.quantity + fill_price * quantity
            ) / total_quantity
            self._position = replace(
                prior,
                entry_price=average_price,
                quantity=total_quantity,
                entry_commission=prior.entry_commission + fee,
                margin=prior.margin + margin,
            )
        remaining = max(0.0, o.quantity - quantity)
        cumulative = o.cumulative_quantity + quantity
        status = "partial" if remaining > 1e-12 else "filled"
        if remaining > 1e-12 and self._execution_proxy is not None:
            self._pending = (
                replace(o, quantity=remaining, cumulative_quantity=cumulative)
                if self._execution_proxy.entry_remainder_policy == "carry"
                else None
            )
        else:
            self._pending = None
        self._entry_bar_ts = int(ts_ms)
        self._last_close = fill_price
        self._last_timestamp_ms = int(ts_ms)
        fill = Fill(
            "entry",
            o.side,
            fill_price,
            quantity,
            int(ts_ms),
            -fee,
            commission=fee,
            reference_price=reference,
            spread_cost=spread,
            slippage_cost=slip,
            margin=margin,
            liquidity_role=liquidity_role if fee_application is not None else None,
            fee_rate_bps=None if fee_application is None else fee_application.rate_bps,
            fee_currency=None if fee_application is None else fee_application.currency,
            fee_evidence_id=None if fee_application is None else fee_application.evidence_id,
            fee_evidence_status=(
                None if fee_application is None else fee_application.evidence_status
            ),
            discount_treatment=(
                None if fee_application is None else fee_application.discount_treatment
            ),
            fee_tier_id=None if fee_application is None else fee_application.tier_id,
            instrument_evidence_id=instrument_evidence_id,
            status=status,
            cumulative_quantity=cumulative,
            order_id=o.client_order_id or "paper-entry",
            execution_model=(
                OBSERVED_QUOTE_EXECUTION_MODEL
                if self._quote_match_active
                else slippage.model
                if slippage is not None
                else "fixed_ohlcv_proxy"
            ),
            impact_evidence_id=None if slippage is None else slippage.evidence_id,
            decision_timestamp_ms=(
                None if o.timeline is None else o.timeline.decision_timestamp_ms
            ),
            submission_timestamp_ms=(
                None if o.timeline is None else o.timeline.submission_timestamp_ms
            ),
            acknowledgement_timestamp_ms=(
                None if o.timeline is None else o.timeline.acknowledgement_timestamp_ms
            ),
            protection_active_timestamp_ms=(
                None if o.timeline is None else o.timeline.protection_active_timestamp_ms
            ),
        )
        self._record_broker_fill(fill, o.session_id, o.client_order_id, o.symbol, role="entry")
        return fill

    def modify_stop(self, new_stop: float) -> None:
        if self._position is not None:
            self.modify_protection(stop_price=new_stop)

    def modify_protection(
        self,
        *,
        stop_price: float | None = None,
        target_price: float | None = None,
    ) -> None:
        p = self._position
        if p is None:
            raise ValueError("no active protection to modify")
        stop = p.stop_price if stop_price is None else float(stop_price)
        target = p.target_price if target_price is None else float(target_price)
        if self._instrument_specs:
            spec = select_instrument_spec(
                self._instrument_specs, timestamp_ms=self._last_timestamp_ms
            )
            stop, target = (
                float(
                    normalize_order(
                        spec,
                        side="sell" if p.side == "buy" else "buy",
                        order_type="stop",
                        quantity=Decimal(str(p.quantity)),
                        price=Decimal(str(price)),
                        reference_price=Decimal(str(self._last_close)),
                    ).price
                )
                for price in (stop, target)
            )
        validate_protection_update(
            side=p.side,
            current_stop=p.stop_price,
            stop_price=stop,
            target_price=target,
        )
        self._position = replace(p, stop_price=stop, target_price=target)
        if self._pending is not None:
            self._pending = replace(self._pending, stop_price=stop, target_price=target)

    @property
    def protection_reference(self) -> BrokerPosition | None:
        """Position at the fill boundary, before any same-bar protective exit."""
        return self._deferred_exit[1] if self._deferred_exit is not None else self._position

    def place_protection(self, intent: ProtectiveOrderIntent) -> list[BrokerOrderAck]:
        self._validate_protection_intent(intent)
        if self._position is not None:
            self._position = replace(
                self._position,
                stop_price=float(intent.stop_price),
                target_price=float(intent.target_price),
                session_id=intent.session_id,
                entry_client_order_id=intent.entry_client_order_id,
                symbol=intent.symbol,
                stop_client_order_id=intent.stop_client_order_id,
                target_client_order_id=intent.target_client_order_id,
            )
        self._record_deferred_exit(intent)
        return [
            BrokerOrderAck(
                session_id=intent.session_id,
                client_order_id=intent.stop_client_order_id,
                exchange_order_id=None,
                status="accepted",
                target=self.target,
                metadata={"paper": True, "reduce_only": intent.reduce_only},
            ),
            BrokerOrderAck(
                session_id=intent.session_id,
                client_order_id=intent.target_client_order_id,
                exchange_order_id=None,
                status="accepted",
                target=self.target,
                metadata={"paper": True, "reduce_only": intent.reduce_only},
            ),
        ]

    def _validate_protection_intent(self, intent: ProtectiveOrderIntent) -> BrokerPosition:
        reference = self.protection_reference
        if reference is None:
            raise ValueError("invalid protective order: no position to protect")
        if intent.target != self.target:
            raise ValueError("invalid protective order: target does not match the broker")
        if intent.side not in {"buy", "sell"} or intent.side != reference.side:
            raise ValueError("invalid protective order: side does not match the position")
        if not intent.reduce_only:
            raise ValueError("invalid protective order: reduce_only must be true")
        identifiers = (
            intent.session_id,
            intent.entry_client_order_id,
            intent.stop_client_order_id,
            intent.target_client_order_id,
            intent.symbol,
        )
        if any(not value for value in identifiers) or len(set(identifiers[1:4])) != 3:
            raise ValueError(
                "invalid protective order: identifiers and symbol are required and unique"
            )
        if reference.session_id and intent.session_id != reference.session_id:
            raise ValueError("invalid protective order: session does not match the position")
        if (
            reference.entry_client_order_id
            and intent.entry_client_order_id != reference.entry_client_order_id
        ):
            raise ValueError("invalid protective order: entry does not match the position")
        if reference.symbol and intent.symbol != reference.symbol:
            raise ValueError("invalid protective order: symbol does not match the position")
        try:
            quantity = float(intent.quantity)
            stop_price = float(intent.stop_price)
            target_price = float(intent.target_price)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "invalid protective order: prices and quantity must be numeric"
            ) from exc
        if any(
            not math.isfinite(value) or value <= 0 for value in (quantity, stop_price, target_price)
        ):
            raise ValueError(
                "invalid protective order: prices and quantity must be positive and finite"
            )
        if quantity != reference.quantity:
            raise ValueError("invalid protective order: quantity does not match the position")
        if stop_price == reference.stop_price and target_price == reference.target_price:
            # These validated legs already participated in paper matching. A gap
            # or partial exit must not reinterpret them as a new bracket.
            return reference
        if reference.stop_client_order_id and reference.target_client_order_id:
            # Resizing an already-protected partial position must preserve a
            # profit-locking stop even when it lies beyond the average entry.
            validate_protection_update(
                side=reference.side,
                current_stop=reference.stop_price,
                stop_price=stop_price,
                target_price=target_price,
            )
            return reference
        if intent.side == "buy":
            if stop_price < reference.stop_price:
                raise ValueError("invalid protective order: stop must not increase position risk")
            if not stop_price < reference.entry_price < target_price:
                raise ValueError(
                    "invalid protective order: long protection must satisfy stop < entry < target"
                )
        else:
            if stop_price > reference.stop_price:
                raise ValueError("invalid protective order: stop must not increase position risk")
            if not target_price < reference.entry_price < stop_price:
                raise ValueError(
                    "invalid protective order: short protection must satisfy target < entry < stop"
                )
        return reference

    def process_bar(  # noqa: A002
        self,
        *,
        ts_ms: int,
        open: float,  # noqa: A002
        high: float,
        low: float,
        close: float,
        volume: float | None = None,
        _quote_observation: bool = False,
    ) -> list[Fill]:
        if self._observed_quote_execution and not _quote_observation:
            self._last_bar_timestamp_ms = int(ts_ms)
            self._settle_funding(int(ts_ms))
            # A closed candle can update the account mark, but cannot execute an
            # order or rewind a later quote observation.
            if int(ts_ms) >= self._last_timestamp_ms:
                self._last_close = float(close)
                self._last_timestamp_ms = int(ts_ms)
            return []
        liquidity = self._liquidity_budget(timestamp_ms=int(ts_ms), volume=volume)
        if not _quote_observation:
            self._settle_funding(int(ts_ms))
        liquidation = None if _quote_observation else self._liquidate_if_required(int(ts_ms))
        if liquidation is not None:
            self._pending = None
            return [liquidation]
        self._last_close = float(close)
        self._last_timestamp_ms = int(ts_ms)

        # Existing protective orders consume the shared bar budget first. If
        # protection trades, an unfilled entry remainder is canceled before it
        # can increase exposure again.
        if self._position is not None:
            if not (self._profile.delays_protection and self._entry_bar_ts == int(ts_ms)):
                exit_fill = self._exit_cross(
                    open=open,
                    high=high,
                    low=low,
                    ts_ms=ts_ms,
                    allow_open_gap=True,
                    liquidity=liquidity,
                    volume=volume,
                )
                if exit_fill is not None:
                    self._pending = None
                    return [exit_fill]

        # A carried remainder may coexist with its partially opened position.
        if self._pending is not None:
            o = self._pending
            if o.timeline is not None and int(ts_ms) < o.timeline.fill_eligible_timestamp_ms:
                return []
            instrument_spec: InstrumentSpecEvidence | None = None
            if self._instrument_specs:
                try:
                    instrument_spec = select_instrument_spec(
                        self._instrument_specs, timestamp_ms=int(ts_ms)
                    )
                    o = self._normalize_pending_order(
                        o, instrument_spec, reference_price=float(open)
                    )
                except ValueError as exc:
                    self._pending = None
                    self._entry_rejection = f"instrument_constraint: {exc}"
                    return []
            matched = (
                float(open)
                if o.order_type in _MARKET_ORDER_TYPES and self._profile.is_costed
                else self._entry_cross(o, open, high, low)
            )
            if matched is None:
                return []
            liquidity_role = "maker" if o.order_type in _LIMIT_ORDER_TYPES else "taker"
            # Inspect capacity first; only an accepted, normalized fill consumes it.
            fill_quantity = (
                o.quantity if liquidity is None else min(o.quantity, float(liquidity.remaining))
            )
            if fill_quantity <= 0:
                return []
            slippage = self._slippage_resolution(
                timestamp_ms=(
                    int(ts_ms) if o.timeline is None else o.timeline.decision_timestamp_ms
                ),
                fill_quantity=fill_quantity,
                volume=volume,
            )
            fill_price = matched
            if self._profile.is_costed:
                limit = o.entry_price if o.order_type in _LIMIT_ORDER_TYPES else None
                fill_price = adjusted_price(
                    matched,
                    side=o.side,
                    fraction=self._adjustment_fraction(slippage),
                    limit=limit,
                )
                if instrument_spec is not None:
                    o, fill_price = self._normalize_fill(o, fill_price, instrument_spec)
                o = self._risk_adjusted_order(
                    o,
                    fill_price,
                    liquidity_role=liquidity_role,
                    adjustment_fraction=(
                        self._profile.adjustment_fraction
                        if self._quote_match_active
                        else self._adjustment_fraction(slippage)
                    ),
                )
                fill_quantity = min(fill_quantity, o.quantity)
                if fill_quantity <= 0:
                    self._pending = None
                    if self._position is None:
                        self._entry_rejection = "risk_budget_exhausted"
                    return []
                if instrument_spec is not None:
                    try:
                        o, fill_price = self._normalize_fill(o, fill_price, instrument_spec)
                        normalized_fill, _ = self._normalize_fill(
                            replace(o, quantity=min(fill_quantity, o.quantity)),
                            fill_price,
                            instrument_spec,
                        )
                        fill_quantity = normalized_fill.quantity
                    except ValueError as exc:
                        self._pending = None
                        self._entry_rejection = f"instrument_constraint: {exc}"
                        return []
                if self._reject_unaffordable_entry(
                    fill_quantity, fill_price, liquidity_role=liquidity_role, margin_mark=matched
                ):
                    return []
            self._allocate_fill_quantity(
                requested=fill_quantity,
                order_id=o.client_order_id or "paper-entry",
                liquidity=liquidity,
            )
            entry_fill = self._open(
                o,
                reference=matched,
                fill_price=fill_price,
                ts_ms=ts_ms,
                liquidity_role=liquidity_role,
                fill_quantity=fill_quantity,
                slippage=slippage,
            )
            self._last_close = float(close)
            self._last_timestamp_ms = int(ts_ms)
            if self._profile.delays_protection:
                return [entry_fill]
            protection_breach = self._entry_fill_protection_breach(entry_fill, o)
            if protection_breach is not None and self._position is not None:
                exit_fill = self._close(
                    self._position,
                    reference=matched,
                    kind=protection_breach,
                    ts_ms=ts_ms,
                    liquidity=liquidity,
                    volume=volume,
                )
                if exit_fill is not None:
                    self._pending = None
                    return [entry_fill, exit_fill]
            if self._position is not None and (
                o.timeline is None or int(ts_ms) >= o.timeline.protection_active_timestamp_ms
            ):
                exit_fill = self._exit_cross(
                    open=open,
                    high=high,
                    low=low,
                    ts_ms=ts_ms,
                    allow_open_gap=False,
                    liquidity=liquidity,
                    volume=volume,
                )
                if exit_fill is not None:
                    self._pending = None
                    return [entry_fill, exit_fill]
            return [entry_fill]
        return []

    def process_observed_quote(
        self,
        *,
        received_at_ms: int,
        venue_event_ms: int,
        bid: float,
        ask: float,
        order_received_at_ms: int | None,
    ) -> list[Fill]:
        """Match a live paper order against a fresh, post-intent executable side.

        The caller archives the complete public observation before invoking this
        method. A quote is a sampled price, not a claim that the venue filled an
        order or that no protective level was crossed between samples.
        """
        if not self._observed_quote_execution:
            raise ValueError("observed quote execution is not enabled")
        timestamps = (received_at_ms, venue_event_ms)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in timestamps
        ):
            raise ValueError("quote timestamps must be non-negative integers")
        if (
            self._last_observed_quote_ms is not None
            and received_at_ms <= self._last_observed_quote_ms
        ):
            raise ValueError("duplicate or regressing quote observation")
        if (
            venue_event_ms > received_at_ms
            or received_at_ms - venue_event_ms > MAX_OBSERVED_QUOTE_AGE_MS
        ):
            raise ValueError("stale or future quote observation")
        if not math.isfinite(float(bid)) or not math.isfinite(float(ask)) or bid <= 0 or ask <= bid:
            raise ValueError("quote must have a positive uncrossed bid and ask")
        if self._pending is not None and (
            order_received_at_ms is None
            or venue_event_ms < order_received_at_ms
            or received_at_ms < order_received_at_ms
        ):
            raise ValueError("quote precedes order receipt")
        self._last_observed_quote_ms = received_at_ms
        if self._position is not None:
            executable = bid if self._position.side == "buy" else ask
        elif self._pending is not None:
            executable = ask if self._pending.side == "buy" else bid
            expected_fill = adjusted_price(
                executable,
                side=self._pending.side,
                fraction=self._profile.slippage_bps / 10_000,
                limit=(
                    self._pending.entry_price
                    if self._pending.order_type in _LIMIT_ORDER_TYPES
                    else None
                ),
            )
            if (
                self._entry_fill_protection_breach(
                    Fill(
                        kind="entry",
                        side=self._pending.side,
                        price=expected_fill,
                        quantity=self._pending.quantity,
                        timestamp_ms=received_at_ms,
                        realized_pnl=0.0,
                    ),
                    self._pending,
                )
                is not None
            ):
                self._pending = None
                self._entry_rejection = "quote_outside_protection"
                return []
        else:
            return []
        self._quote_match_active = True
        try:
            fills = self.process_bar(
                ts_ms=received_at_ms,
                open=executable,
                high=executable,
                low=executable,
                close=executable,
                _quote_observation=True,
            )
            if self._position is not None:
                self._last_close = bid if self._position.side == "buy" else ask
            return fills
        finally:
            self._quote_match_active = False

    @staticmethod
    def _entry_fill_protection_breach(fill: Fill, order: _PendingOrder) -> str | None:
        """Classify a fill already beyond its bracket for immediate market containment."""
        if order.side == "buy":
            if fill.price <= order.stop_price:
                return "stop_loss"
            if fill.price >= order.target_price:
                return "take_profit"
        else:
            if fill.price >= order.stop_price:
                return "stop_loss"
            if fill.price <= order.target_price:
                return "take_profit"
        return None

    def _settle_funding(self, timestamp_ms: int) -> None:
        if self._funding is None:
            return
        if not self._funding.requested_start_ms <= timestamp_ms <= self._funding.requested_end_ms:
            raise ValueError("funding evidence does not cover the bar timestamp")
        # Resume where the last bar stopped. Rescanning from the first record
        # every bar would make a run quadratic in the number of settlements.
        records = self._funding.records
        while self._funding_cursor < len(records):
            record = records[self._funding_cursor]
            if record.settlement_timestamp_ms > timestamp_ms:
                break
            self._funding_cursor += 1
            self._apply_funding_record(record)

    def _apply_funding_record(self, record) -> None:
        if (
            self._last_funding_timestamp_ms is not None
            and record.settlement_timestamp_ms <= self._last_funding_timestamp_ms
        ):
            return
        self._last_funding_timestamp_ms = record.settlement_timestamp_ms
        position = self._position
        if position is None:
            return
        if (
            self._observed_quote_execution
            and record.settlement_timestamp_ms <= position.entry_timestamp_ms
        ):
            # A delayed closed bar can report a real funding settlement from
            # before this position existed. Do not charge the new position.
            return
        amount = funding_cashflow(
            side=position.side,
            quantity=Decimal(str(position.quantity)),
            rate=record.rate,
            mark_price=record.settlement_mark_price,
        )
        self._ledger.record(
            timestamp_ms=record.settlement_timestamp_ms,
            kind="funding",
            amount=float(amount),
            reference_id=f"funding-{record.settlement_timestamp_ms}",
            metadata={
                "rate": str(record.rate),
                "mark_price": str(record.settlement_mark_price),
                "source": record.source,
            },
        )

    def _liquidity_budget(
        self, *, timestamp_ms: int, volume: float | None
    ) -> BarLiquidityBudget | None:
        if self._quote_match_active:
            # The archived top-of-book supplies the executable price. OHLCV
            # bar-volume participation is not evidence of displayed depth.
            return None
        proxy = self._execution_proxy
        if proxy is None:
            return None
        if volume is None or not math.isfinite(float(volume)) or float(volume) < 0:
            raise ValueError("execution proxy requires finite non-negative bar volume")
        return BarLiquidityBudget(
            timestamp_ms=timestamp_ms,
            observed_volume=Decimal(str(volume)),
            maximum_participation=proxy.maximum_volume_participation,
        )

    @staticmethod
    def _allocate_fill_quantity(
        *,
        requested: float,
        order_id: str,
        liquidity: BarLiquidityBudget | None,
    ) -> float:
        if liquidity is None:
            return float(requested)
        return float(liquidity.allocate(order_id=order_id, requested=Decimal(str(requested))))

    def _slippage_resolution(
        self,
        *,
        timestamp_ms: int,
        fill_quantity: float,
        volume: float | None,
    ) -> SlippageResolution:
        proxy = self._execution_proxy
        participation = (
            Decimal("0")
            if proxy is None or volume in {None, 0}
            else min(
                Decimal("1"),
                Decimal(str(fill_quantity)) / Decimal(str(volume)),
            )
        )
        return resolve_slippage_bps(
            fixed_slippage_bps=Decimal(str(self._profile.slippage_bps)),
            participation=participation,
            decision_timestamp_ms=timestamp_ms,
            calibration=(None if proxy is None or volume in {None, 0} else proxy.calibration),
        )

    def _adjustment_fraction(self, slippage: SlippageResolution) -> float:
        spread = 0.0 if self._quote_match_active else self._profile.spread_bps / 2
        return (spread + float(slippage.slippage_bps)) / 10_000

    def _normalize_pending_order(
        self,
        order: _PendingOrder,
        spec: InstrumentSpecEvidence,
        *,
        reference_price: float,
    ) -> _PendingOrder:
        if order.symbol:
            normalized_symbol = order.symbol.replace("/", "").replace("_", "").upper()
            if normalized_symbol != spec.canonical_symbol:
                raise ValueError("order symbol does not match instrument evidence")
        entry = normalize_order(
            spec,
            side=order.side,
            order_type=order.order_type,
            quantity=Decimal(str(order.quantity)),
            price=Decimal(str(order.entry_price)),
            reference_price=Decimal(str(reference_price)),
        )
        validate_initial_leverage(
            spec,
            notional=entry.notional,
            leverage=Decimal(str(self._profile.leverage)),
        )
        closing_side = "sell" if order.side == "buy" else "buy"
        stop = normalize_order(
            spec,
            side=closing_side,
            order_type="stop",
            quantity=entry.quantity,
            price=Decimal(str(order.stop_price)),
            reference_price=Decimal(str(reference_price)),
        )
        target = normalize_order(
            spec,
            side=closing_side,
            order_type="stop",
            quantity=entry.quantity,
            price=Decimal(str(order.target_price)),
            reference_price=Decimal(str(reference_price)),
        )
        return replace(
            order,
            entry_price=float(entry.price),
            stop_price=float(stop.price),
            target_price=float(target.price),
            quantity=float(entry.quantity),
        )

    def _normalize_fill(
        self,
        order: _PendingOrder,
        fill_price: float,
        spec: InstrumentSpecEvidence,
    ) -> tuple[_PendingOrder, float]:
        normalized = normalize_order(
            spec,
            side=order.side,
            order_type=order.order_type,
            quantity=Decimal(str(order.quantity)),
            price=Decimal(str(fill_price)),
        )
        validate_initial_leverage(
            spec,
            notional=normalized.notional,
            leverage=Decimal(str(self._profile.leverage)),
        )
        return replace(order, quantity=float(normalized.quantity)), float(normalized.price)

    def _liquidate_if_required(self, timestamp_ms: int) -> Fill | None:
        position = self._position
        if position is None:
            return None
        mark = self._live_mark_prices.get(timestamp_ms)
        if mark is None and self._mark_prices is not None:
            mark = self._mark_prices.at(timestamp_ms)
        if mark is None:
            if not self._live_evidence_enabled and self._mark_prices is None:
                return None
            raise ValueError(f"no mark price evidence for open position at {timestamp_ms}")
        spec = select_instrument_spec(self._instrument_specs, timestamp_ms=timestamp_ms)
        state = evaluate_liquidation(
            spec,
            side=position.side,
            quantity=Decimal(str(position.quantity)),
            entry_price=Decimal(str(position.entry_price)),
            cash_balance=Decimal(str(self.balance)),
            mark_price=mark.price,
        )
        if not state.liquidated:
            return None
        sign = 1.0 if position.side == "buy" else -1.0
        fill_price = float(mark.price)
        gross = (fill_price - position.entry_price) * position.quantity * sign
        liquidation_fee = float(
            state.notional * Decimal(str(spec.liquidation_fee_bps)) / Decimal("10000")
        )
        metadata = {
            "instrument_evidence_id": spec.evidence_id,
            "evidence_status": spec.evidence_status,
            "source": spec.source,
            "mark_price_source": mark.source,
            "maintenance_margin": str(state.maintenance_margin),
        }
        self._ledger.record(
            timestamp_ms=timestamp_ms,
            kind="trade_pnl",
            amount=gross,
            reference_id=position.entry_client_order_id,
            metadata=metadata,
        )
        self._ledger.record(
            timestamp_ms=timestamp_ms,
            kind="liquidation_fee",
            amount=-liquidation_fee,
            reference_id=position.entry_client_order_id,
            currency=spec.collateral_currency,
            metadata={
                **metadata,
                "rate_bps": str(spec.liquidation_fee_bps),
            },
        )
        self._position = None
        self._entry_bar_ts = None
        self._last_close = fill_price
        self._last_timestamp_ms = timestamp_ms
        fill = Fill(
            kind="liquidation",
            side=position.side,
            price=fill_price,
            quantity=position.quantity,
            timestamp_ms=timestamp_ms,
            realized_pnl=gross - liquidation_fee,
            reference_price=fill_price,
            fee_currency=spec.collateral_currency,
            fee_evidence_id=spec.evidence_id,
            fee_evidence_status=spec.evidence_status,
            instrument_evidence_id=spec.evidence_id,
            liquidation_fee=liquidation_fee,
        )
        self._record_close_broker_fill(fill, position)
        return fill

    def _exit_cross(
        self,
        *,
        open: float,
        high: float,
        low: float,
        ts_ms: int,
        allow_open_gap: bool,
        liquidity: BarLiquidityBudget | None = None,
        volume: float | None = None,
    ) -> Fill | None:
        p = self._position
        if p is None:
            return None
        if (
            p.protection_active_timestamp_ms is not None
            and int(ts_ms) < p.protection_active_timestamp_ms
        ):
            return None
        is_long = p.side == "buy"
        if allow_open_gap and (
            (is_long and open <= p.stop_price) or (not is_long and open >= p.stop_price)
        ):
            return self._close(
                p,
                reference=float(open),
                kind="stop_loss",
                ts_ms=ts_ms,
                liquidity=liquidity,
                volume=volume,
            )
        hit_sl = low <= p.stop_price if is_long else high >= p.stop_price
        hit_tp = high >= p.target_price if is_long else low <= p.target_price
        if hit_sl and hit_tp and self._profile.ambiguity_policy is not None:
            self._ambiguities.append(
                {
                    "timestamp_ms": int(ts_ms),
                    "reason_code": "both_protective_levels_touched",
                    "selected": "stop_loss",
                }
            )
        if hit_sl:
            return self._close(
                p,
                reference=p.stop_price,
                kind="stop_loss",
                ts_ms=ts_ms,
                liquidity=liquidity,
                volume=volume,
            )
        if hit_tp:
            # A bar that opens beyond the target is filled at that open.
            favorable_open = (is_long and open >= p.target_price) or (
                not is_long and open <= p.target_price
            )
            reference = float(open) if favorable_open else p.target_price
            return self._close(
                p,
                reference=reference,
                kind="take_profit",
                ts_ms=ts_ms,
                liquidity=liquidity,
                volume=volume,
            )
        return None

    def _risk_adjusted_order(
        self,
        order: _PendingOrder,
        fill_price: float,
        *,
        liquidity_role: str,
        adjustment_fraction: float | None = None,
    ) -> _PendingOrder:
        if self._profile.ambiguity_policy is None or order.risk_budget is None:
            return order
        adjustment = (
            self._profile.adjustment_fraction
            if adjustment_fraction is None
            else adjustment_fraction
        )
        remaining_risk_budget = float(order.risk_budget)
        position = self._position
        stop_reference = order.stop_price
        if position is not None:
            stop_reference = position.stop_price
            closing_side = "sell" if position.side == "buy" else "buy"
            stop_fill = adjusted_price(
                stop_reference,
                side=closing_side,
                fraction=adjustment,
            )
            existing_price_risk = abs(position.entry_price - stop_fill) * position.quantity
            existing_exit_fee = commission(
                position.quantity,
                stop_fill,
                commission_bps=self._fee_rate("taker"),
            )
            remaining_risk_budget -= (
                existing_price_risk + position.entry_commission + existing_exit_fee
            )
        quantity = max_quantity_for_stop_risk(
            risk_budget=max(0.0, remaining_risk_budget),
            entry_fill=fill_price,
            stop_reference=stop_reference,
            position_side=order.side,
            adjustment_fraction=adjustment,
            entry_commission_bps=self._fee_rate(liquidity_role),
            exit_commission_bps=self._fee_rate("taker"),
        )
        return replace(order, quantity=min(order.quantity, quantity))

    def _entry_cross(
        self,
        o: _PendingOrder,
        open: float,
        high: float,
        low: float,
    ) -> float | None:
        if o.order_type in _LIMIT_ORDER_TYPES:
            favorable_open = (o.side == "buy" and open <= o.entry_price) or (
                o.side == "sell" and open >= o.entry_price
            )
            if favorable_open:
                return float(open)
            return o.entry_price if low <= o.entry_price <= high else None
        if o.order_type in _STOP_ORDER_TYPES:
            if (o.side == "buy" and open >= o.entry_price) or (
                o.side == "sell" and open <= o.entry_price
            ):
                return float(open)
            crossed = high >= o.entry_price if o.side == "buy" else low <= o.entry_price
            return o.entry_price if crossed else None
        return None

    def _close(
        self,
        p: BrokerPosition,
        *,
        reference: float,
        kind: str,
        ts_ms: int,
        liquidity: BarLiquidityBudget | None = None,
        volume: float | None = None,
    ) -> Fill | None:
        closing_side = "sell" if p.side == "buy" else "buy"
        order_id = {
            "stop_loss": p.stop_client_order_id or f"{p.entry_client_order_id}-stop",
            "take_profit": p.target_client_order_id or f"{p.entry_client_order_id}-target",
            "manual_close": f"paper-flatten-{p.session_id}",
        }[kind]
        quantity = p.quantity if liquidity is None else min(p.quantity, float(liquidity.remaining))
        if self._instrument_specs:
            spec = select_instrument_spec(self._instrument_specs, timestamp_ms=int(ts_ms))
            step = float(spec.step_size)
            lots = quantity / step
            nearest = round(lots)
            count = (
                nearest
                if math.isclose(lots, nearest, rel_tol=0, abs_tol=1e-9)
                else math.floor(lots)
            )
            quantity = min(quantity, float(Decimal(count) * spec.step_size))
        if quantity <= 0:
            return None
        self._allocate_fill_quantity(requested=quantity, order_id=order_id, liquidity=liquidity)
        slippage = self._slippage_resolution(
            timestamp_ms=int(ts_ms), fill_quantity=quantity, volume=volume
        )
        fill_price = float(reference)
        fee = spread = slip = 0.0
        fee_application: FeeApplication | None = None
        instrument_evidence_id = (
            select_instrument_spec(self._instrument_specs, timestamp_ms=int(ts_ms)).evidence_id
            if self._instrument_specs
            else None
        )
        if self._profile.is_costed:
            fill_price = adjusted_price(
                reference,
                side=closing_side,
                fraction=self._adjustment_fraction(slippage),
            )
            fee_application = self._resolve_fee("taker", ts_ms)
            fee = commission(
                quantity,
                fill_price,
                commission_bps=fee_application.rate_bps,
            )
            spread, slip = cost_split(
                reference,
                fill_price,
                quantity,
                spread_bps=0.0 if self._quote_match_active else self._profile.spread_bps,
                slippage_bps=float(slippage.slippage_bps),
            )
        sign = 1.0 if p.side == "buy" else -1.0
        gross = (fill_price - p.entry_price) * quantity * sign
        cash_effect = gross - fee
        self._ledger.record(
            timestamp_ms=int(ts_ms),
            kind="trade_pnl",
            amount=gross,
            reference_id=p.entry_client_order_id,
        )
        if fee:
            self._ledger.record(
                timestamp_ms=int(ts_ms),
                kind="commission",
                amount=-fee,
                reference_id=p.entry_client_order_id,
                currency=("quote" if fee_application is None else fee_application.currency),
                metadata=({} if fee_application is None else self._fee_metadata(fee_application)),
            )
        remaining = max(0.0, p.quantity - quantity)
        self._exit_cumulative_quantity += quantity
        cumulative = self._exit_cumulative_quantity
        status = "partial" if remaining > 1e-12 else "filled"
        if remaining > 1e-12:
            ratio = remaining / p.quantity
            self._position = replace(
                p,
                quantity=remaining,
                margin=p.margin * ratio,
            )
        else:
            self._position = None
            self._entry_bar_ts = None
            self._exit_cumulative_quantity = 0.0
        # process_bar owns the closing mark; an exit price values only its fill.
        if self._position is None:
            self._last_close = float(fill_price)
        self._last_timestamp_ms = int(ts_ms)
        fill = Fill(
            kind,
            p.side,
            float(fill_price),
            quantity,
            int(ts_ms),
            cash_effect,
            commission=fee,
            reference_price=float(reference),
            spread_cost=spread,
            slippage_cost=slip,
            liquidity_role="taker" if fee_application is not None else None,
            fee_rate_bps=None if fee_application is None else fee_application.rate_bps,
            fee_currency=None if fee_application is None else fee_application.currency,
            fee_evidence_id=None if fee_application is None else fee_application.evidence_id,
            fee_evidence_status=(
                None if fee_application is None else fee_application.evidence_status
            ),
            discount_treatment=(
                None if fee_application is None else fee_application.discount_treatment
            ),
            fee_tier_id=None if fee_application is None else fee_application.tier_id,
            instrument_evidence_id=instrument_evidence_id,
            status=status,
            cumulative_quantity=cumulative,
            order_id=order_id,
            execution_model=(
                OBSERVED_QUOTE_EXECUTION_MODEL if self._quote_match_active else slippage.model
            ),
            impact_evidence_id=slippage.evidence_id,
        )
        self._record_close_broker_fill(fill, p)
        return fill

    def flatten(
        self, *args, ts_ms: int | None = None, price: float | None = None
    ) -> Fill | BrokerOrderAck | None:
        if len(args) == 2 and ts_ms is None and price is None:
            session_id = str(args[1])
            self._pending = None
            if self._position is None:
                return None
            client_order_id = f"paper-flatten-{session_id}"
            fill = self._close(
                self._position,
                reference=self._last_close,
                kind="manual_close",
                ts_ms=self._last_timestamp_ms,
            )
            return BrokerOrderAck(
                session_id=session_id,
                client_order_id=client_order_id,
                exchange_order_id=None,
                status="filled",
                target=self.target,
                metadata={"paper": True, "fill_price": fill.price},
            )
        if ts_ms is None or price is None:
            raise TypeError("paper flatten requires either (symbol, session_id) or ts_ms/price")
        self._pending = None
        if self._position is None:
            return None
        return self._close(self._position, reference=float(price), kind="manual_close", ts_ms=ts_ms)

    def poll_fills(self, session_id: str) -> list[BrokerFill]:
        fills = [fill for fill in self._fill_events if fill.session_id == session_id]
        self._fill_events = [fill for fill in self._fill_events if fill.session_id != session_id]
        return fills

    def reconcile(
        self, session_id: str, intents: list[BrokerOrderIntent]
    ) -> BrokerReconciliationReport:
        open_orders: list[BrokerOrderAck] = []
        pending = self._pending
        if pending is not None and pending.session_id == session_id:
            open_orders.append(
                BrokerOrderAck(
                    session_id=session_id,
                    client_order_id=pending.client_order_id,
                    exchange_order_id=None,
                    status="partial" if pending.cumulative_quantity else "accepted",
                    target=self.target,
                    metadata={
                        "paper": True,
                        "remaining_quantity": str(pending.quantity),
                        "cumulative_quantity": str(pending.cumulative_quantity),
                        "requested_quantity": str(pending.requested_quantity),
                    },
                )
            )
        positions: list[BrokerPositionSnapshot] = []
        position = self._position
        if position is not None and position.session_id == session_id:
            positions.append(
                BrokerPositionSnapshot(
                    symbol=position.symbol,
                    side=position.side,
                    quantity=str(position.quantity),
                    entry_price=str(position.entry_price),
                    position_id=position.entry_client_order_id,
                    metadata={
                        "paper": True,
                        "margin": position.margin,
                        "stop_price": position.stop_price,
                        "target_price": position.target_price,
                    },
                )
            )
        reconciliation = self.reconcile_ledger()
        return BrokerReconciliationReport(
            session_id=session_id,
            target=self.target,
            open_orders=open_orders,
            fills=[fill for fill in self._fill_events if fill.session_id == session_id],
            positions=positions,
            metadata={
                "ledger_balanced": reconciliation.balanced,
                "execution_model": self.resolved_metadata["execution_model"],
            },
        )

    def cancel_all(self, symbol: str, session_id: str) -> list[BrokerOrderAck]:
        had_pending = self._pending is not None
        self._pending = None
        return [
            BrokerOrderAck(
                session_id=session_id,
                client_order_id=f"paper-cancel-{session_id}",
                exchange_order_id=None,
                status="canceled" if had_pending else "accepted",
                target=self.target,
                metadata={"paper": True, "symbol": symbol},
            )
        ]

    def _record_close_broker_fill(self, fill: Fill, position: BrokerPosition) -> None:
        role = {
            "stop_loss": "stop",
            "take_profit": "target",
            "manual_close": "flatten",
            "liquidation": "flatten",
        }[fill.kind]
        client_order_id = {
            "stop": position.stop_client_order_id,
            "target": position.target_client_order_id,
            "flatten": f"paper-flatten-{position.session_id}",
        }[role]
        if role in {"stop", "target"} and not client_order_id:
            if position.session_id and position.entry_client_order_id:
                self._deferred_exit = (fill, position, role)
            return
        self._record_broker_fill(
            fill,
            position.session_id,
            client_order_id,
            position.symbol,
            role=role,
        )

    def _record_deferred_exit(self, intent: ProtectiveOrderIntent) -> None:
        deferred = self._deferred_exit
        if deferred is None:
            return
        fill, position, role = deferred
        if (
            position.session_id != intent.session_id
            or position.entry_client_order_id != intent.entry_client_order_id
        ):
            return
        client_order_id = (
            intent.stop_client_order_id if role == "stop" else intent.target_client_order_id
        )
        self._record_broker_fill(
            fill,
            intent.session_id,
            client_order_id,
            intent.symbol,
            role=role,
        )
        self._deferred_exit = None

    def _record_broker_fill(
        self,
        fill: Fill,
        session_id: str,
        client_order_id: str,
        symbol: str,
        *,
        role: str,
    ) -> None:
        if not session_id or not client_order_id:
            return
        self._fill_events.append(
            BrokerFill(
                session_id=session_id,
                client_order_id=client_order_id,
                exchange_order_id=None,
                symbol=symbol,
                side=fill.side,
                status=fill.status,  # type: ignore[arg-type]
                role=role,  # type: ignore[arg-type]
                quantity=str(fill.quantity),
                price=str(fill.price),
                timestamp_ms=fill.timestamp_ms,
                realized_pnl=str(fill.realized_pnl),
                metadata={
                    "paper": True,
                    "cashflow_sequences": [
                        entry.sequence
                        for entry in self.ledger.entries
                        if entry.sequence > self._journaled_ledger_sequence
                        and entry.kind != "funding"
                    ],
                    "kind": fill.kind,
                    "commission": fill.commission,
                    "liquidity_role": fill.liquidity_role,
                    "fee_rate_bps": fill.fee_rate_bps,
                    "fee_currency": fill.fee_currency,
                    "fee_evidence_id": fill.fee_evidence_id,
                    "fee_evidence_status": fill.fee_evidence_status,
                    "discount_treatment": fill.discount_treatment,
                    "fee_tier_id": fill.fee_tier_id,
                    "instrument_evidence_id": fill.instrument_evidence_id,
                    "liquidation_fee": fill.liquidation_fee,
                    "cumulative_quantity": fill.cumulative_quantity,
                    "order_id": fill.order_id,
                    "execution_model": fill.execution_model,
                    "impact_evidence_id": fill.impact_evidence_id,
                    "decision_timestamp_ms": fill.decision_timestamp_ms,
                    "submission_timestamp_ms": fill.submission_timestamp_ms,
                    "acknowledgement_timestamp_ms": fill.acknowledgement_timestamp_ms,
                    "protection_active_timestamp_ms": fill.protection_active_timestamp_ms,
                    "reference_price": fill.reference_price,
                    "spread_cost": fill.spread_cost,
                    "slippage_cost": fill.slippage_cost,
                },
            )
        )

        self._journaled_ledger_sequence = len(self.ledger.entries)


def _optional_positive_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _optional_timestamp_ms(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
