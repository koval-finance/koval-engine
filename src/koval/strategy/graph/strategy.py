"""Bridge the typed executor to the DeclarativeStrategy contract the BT adapter
drives. One executor per strategy instance (parallel-run safe)."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from koval.engine.account_state import PlatformAccountState
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup
from koval.strategy.block_assembler import GraphValidationError
from koval.strategy.graph.evidence import DecisionRecorder
from koval.strategy.graph.executor import GraphExecutor, StepResult
from koval.strategy.graph.indicators import PreparedIndicators
from koval.strategy.graph.node import BarContext
from koval.strategy.helpers.exits.trailing import trailing_stop_price


def build_graph_strategy(
    typed_graph: dict, dynamic_exit_fn: Callable | None = None
) -> type[DeclarativeStrategy]:
    if dynamic_exit_fn is not None and any(
        b.get("type") == "exec.order_constructor" and (b.get("params") or {}).get("trail_pct")
        for b in typed_graph.get("blocks") or []
    ):
        raise GraphValidationError(
            "Graph sets exec.order_constructor trail_pct and also carries a legacy "
            "dynamic_exit block; use one stop-moving mechanism"
        )

    class _GraphStrategy(DeclarativeStrategy):
        def __init__(self) -> None:
            super().__init__()
            self._executor = GraphExecutor.build(typed_graph)
            self._stepped_bar = -1
            self._result: StepResult | None = None
            # Dynamic-exit (trailing / breakeven) is carried here for the compat
            # path: the legacy callable + the live bracket drive on_sl_update.
            self._dyn_exit = dynamic_exit_fn
            self._open_trade_setup: TradeSetup | None = None
            self._open_current_stop: float = 0.0
            # Platform Account State: per-run, parallel-safe. Seeded from the
            # first bar's equity; fed by on_bar + position hooks.
            self._account = PlatformAccountState()
            self._account_seeded = False
            self._open_order_meta: dict = {}
            self._indicators: PreparedIndicators | None = None

        def prepare_backtest(self, candles, *, history_bars: int) -> None:
            """Prepare only pure indicators; graph/account state remains untouched."""
            self._indicators = PreparedIndicators.build(typed_graph, candles, history_bars)

        def _ctx(self) -> BarContext:
            account = self.account_snapshot()
            if account is None:
                if not self._account_seeded:
                    self._account = PlatformAccountState(starting_balance=self.account_value)
                    self._account_seeded = True
                self._account.on_bar(equity=self.account_value, timestamp_ms=self.timestamp_ms)
                account = self._account.snapshot()
            return BarContext(
                close=self.close,
                high=self.high,
                low=self.low,
                open=self.open,
                volume=self.volume,
                bar_index=self.bar_index,
                timestamp_ms=self.timestamp_ms,
                closes=self.closes,
                highs=self.highs,
                lows=self.lows,
                opens=self.opens,
                volumes=self.volumes,
                htf_closes=self.htf_closes,
                htf_highs=self.htf_highs,
                htf_lows=self.htf_lows,
                htf_opens=self.htf_opens,
                htf_volumes=self.htf_volumes,
                account_value=self.account_value,
                position_size=self.position_size,
                position_direction=self.position_direction,
                symbol=str(self.config.get("symbol", "")),
                account=account,
                evidence=DecisionRecorder(),
                decision_timestamp_ms=getattr(self, "decision_timestamp_ms", None),
                history_start_ms=getattr(self, "history_start_ms", None),
                timeframe=self.config.get("timeframe"),
                indicators=None
                if self._indicators is None or self.closes is None
                else self._indicators.at(self.timestamp_ms, len(self.closes)),
            )

        def _ensure_stepped(self) -> StepResult:
            if self.bar_index != self._stepped_bar:
                self._result = self._executor.step(self._ctx())
                self._stepped_bar = self.bar_index
            return self._result  # type: ignore[return-value]

        def _routed_side(self) -> str | None:
            # The terminal (routed) order is the decision to trade. In the native
            # execution pipeline: a risk gate may block the order while the upstream
            # TradingIntent still exists, so should_long/short gate on the ROUTED
            # ORDER, not the intent — otherwise the adapter would call go_long()
            # with no order to build a setup from. For the compat path the
            # order_constructor always emits an order when an intent exists, so
            # this is behaviour-identical there.
            orders = self._ensure_stepped().orders
            return orders[0].side if orders else None

        def should_long(self) -> bool:
            return self._routed_side() == "buy"

        def should_short(self) -> bool:
            return self._routed_side() == "sell"

        def on_bar(self) -> None:
            """Synchronize account state and evaluate the graph once per bar."""
            self._ensure_stepped()

        def _setup(self, direction: str) -> TradeSetup:
            result = self._ensure_stepped()
            orders = result.orders
            if not orders:
                raise RuntimeError("graph produced intent without OrderRequest")
            o = orders[0]
            self._open_order_meta = dict(o.metadata)
            context = result.decision_context or {}
            contributing_ids = {n["runtime_node_id"] for n in context.get("nodes", [])}
            reasoning = next(
                (
                    intent.metadata.get("reasoning_chain", "")
                    for intent in result.intents
                    if intent.source_node_id in contributing_ids
                ),
                "",
            )
            observed_reasons = [
                f"{n['node_type']} ({n['node_id']}): {n['predicate']}"
                for n in sorted(context.get("nodes", []), key=lambda n: n["role"] != "fact")
                if n["result"] == "passed" and n["predicate"]
            ]
            setup = TradeSetup(
                direction=direction,
                entry_price=o.entry_price,
                stop_loss=o.stop_price,
                take_profit=o.target_price,
                size=o.quantity,
                entry_type=o.order_type,
                why_entry=[reasoning] if reasoning else observed_reasons,
                decision_context=deepcopy(result.decision_context),
                indicators_at_entry={
                    n["runtime_node_id"]: {**deepcopy(n["params"]), **deepcopy(n["values"])}
                    for n in (result.decision_context or {}).get("nodes", [])
                    if n["values"]
                },
            )
            self._open_trade_setup = setup
            self._open_current_stop = o.stop_price
            return setup

        def go_long(self) -> TradeSetup:
            return self._setup("long")

        def go_short(self) -> TradeSetup:
            return self._setup("short")

        def on_open_position(self, trade_id: int, setup: TradeSetup) -> None:
            self._open_trade_setup = setup
            self._open_current_stop = setup.stop_loss
            if self.account_snapshot() is not None:
                return
            self._account.on_open(
                side="buy" if setup.direction == "long" else "sell",
                entry_price=setup.entry_price,
                quantity=setup.size,
                current_stop=setup.stop_loss,
                margin=float(self._open_order_meta.get("required_margin", 0.0)),
            )

        def on_close_position(self, trade_id: int, result: dict) -> None:
            self._open_trade_setup = None
            self._open_current_stop = 0.0
            if self.account_snapshot() is None:
                self._account.on_close(realized_pnl=float(result.get("pnl", 0.0)))

        def on_sl_update(self, trade_id: int) -> float | None:
            if self._open_trade_setup is None:
                return None
            trail_pct = self._open_order_meta.get("trail_pct")
            if self._dyn_exit is not None:
                new_sl = self._dyn_exit(
                    self,
                    trade_id=trade_id,
                    direction=self._open_trade_setup.direction,
                    entry_price=self._open_trade_setup.entry_price,
                    current_stop=self._open_current_stop,
                )
            elif trail_pct is not None:
                new_sl = trailing_stop_price(
                    direction=self._open_trade_setup.direction,
                    current_price=float(self.close),
                    trail_pct=float(trail_pct),
                    current_stop=self._open_current_stop,
                )
            else:
                return None
            if new_sl is None or new_sl == self._open_current_stop:
                return None
            self._open_current_stop = float(new_sl)
            return float(new_sl)

        def was_blocked(self) -> bool:
            """True when the last step produced an intent but no routed order —
            i.e. a risk gate blocked the entry. Drives the live monitor's
            entries-halted flag."""
            r = self._result
            return bool(r is not None and r.intents and not r.orders)

    return _GraphStrategy
