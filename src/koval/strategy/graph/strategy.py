"""Bridge the typed executor to the DeclarativeStrategy contract the BT adapter
drives. One executor per strategy instance (parallel-run safe)."""

from __future__ import annotations

from collections.abc import Callable

from koval.engine.account_state import PlatformAccountState
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup
from koval.strategy.graph.executor import GraphExecutor, StepResult
from koval.strategy.graph.node import BarContext


def build_graph_strategy(
    typed_graph: dict, dynamic_exit_fn: Callable | None = None
) -> type[DeclarativeStrategy]:
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

        def _ctx(self) -> BarContext:
            if not self._account_seeded:
                self._account = PlatformAccountState(starting_balance=self.account_value)
                self._account_seeded = True
            self._account.on_bar(equity=self.account_value, timestamp_ms=self.timestamp_ms)
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
                account=self._account.snapshot(),
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
            reasoning = ""
            if result.intents:
                reasoning = result.intents[0].metadata.get("reasoning_chain", "")
            setup = TradeSetup(
                direction=direction,
                entry_price=o.entry_price,
                stop_loss=o.stop_price,
                take_profit=o.target_price,
                size=o.quantity,
                entry_type=o.order_type,
                why_entry=[reasoning] if reasoning else [],
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
            self._account.on_close(realized_pnl=float(result.get("pnl", 0.0)))

        def on_sl_update(self, trade_id: int) -> float | None:
            if self._dyn_exit is None or self._open_trade_setup is None:
                return None
            new_sl = self._dyn_exit(
                self,
                trade_id=trade_id,
                direction=self._open_trade_setup.direction,
                entry_price=self._open_trade_setup.entry_price,
                current_stop=self._open_current_stop,
            )
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
