from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from koval.strategy.base.trade_setup import TradeSetup

if TYPE_CHECKING:
    import numpy as np

    from koval.engine.account_state import AccountSnapshot


class DeclarativeStrategy:
    """
    Strategy contract. The engine calls hooks; the strategy returns intent.
    Never call self.buy() / self.sell() here — return TradeSetup instead.
    Concrete backtest-engine mechanics belong in independently installed plugins.

    State fields are injected by the host before each closed-bar evaluation.
    """

    # Market state — current bar (injected by adapter per bar)
    close: float = 0.0
    high: float = 0.0
    low: float = 0.0
    open: float = 0.0
    volume: float = 0.0
    bar_index: int = 0
    timestamp_ms: int = 0

    # Candle history (injected by adapter; chronological — arr[-1] == current bar)
    closes: np.ndarray | None = None
    highs: np.ndarray | None = None
    lows: np.ndarray | None = None
    opens: np.ndarray | None = None
    volumes: np.ndarray | None = None

    # Higher-timeframe history (injected by adapter when a 2nd feed exists)
    htf_closes: np.ndarray | None = None
    htf_highs: np.ndarray | None = None
    htf_lows: np.ndarray | None = None
    htf_opens: np.ndarray | None = None
    htf_volumes: np.ndarray | None = None

    # Account state (injected by adapter)
    account_value: float = 0.0
    position_size: float = 0.0
    position_direction: str | None = None

    # Run config (injected by the host application at run start)
    config: dict

    def __init__(self) -> None:
        self.config = {}
        self._account_provider: Callable[[], AccountSnapshot] | None = None

    def bind_account(self, provider: Callable[[], AccountSnapshot]) -> None:
        """Bind a read-only snapshot provider owned and updated by the runtime."""
        self._account_provider = provider

    def account_snapshot(self) -> AccountSnapshot | None:
        """Return actual account state, or None when running without a host binding."""
        provider = getattr(self, "_account_provider", None)
        return provider() if provider is not None else None

    def should_long(self) -> bool:
        return False

    def should_short(self) -> bool:
        return False

    def go_long(self) -> TradeSetup:
        raise NotImplementedError("Override go_long()")

    def go_short(self) -> TradeSetup:
        raise NotImplementedError("Override go_short()")

    def filters(self) -> list:
        """Return list of zero-arg callables. All must return True to allow entry."""
        return []

    def _execute_filters(self) -> bool:
        return all(f() for f in self.filters())

    def should_cancel_entry(self) -> bool:
        return False

    def on_bar(self) -> None:
        """Evaluate bar-local strategy state after the host injects current data.

        Hosts call this once per closed bar, after candle, account, and position
        fields are synchronized. Stateless strategies may keep the default.
        """

    def on_open_position(self, trade_id: int, setup: TradeSetup) -> None:
        pass

    def on_close_position(self, trade_id: int, result: dict) -> None:
        pass

    def on_sl_update(self, trade_id: int) -> float | None:
        """Return new SL price, or None to keep current."""
        return None

    def on_tp_update(self, trade_id: int) -> float | None:
        """Return a target after bar matching, or None to keep the current level.

        Runtimes snapshot SL and TP hooks together. Targets may move in either
        direction but must remain strictly beyond the final protective stop.
        """
        return None

    @classmethod
    def metadata(cls) -> dict:
        """
        Strategy identity for the registry.
        Override in concrete strategies to provide real values.
        """
        return {
            "name": cls.__name__,
            "description": "",
            "author": "",
            "tags": [],
            "version": "0.1.0",
            "is_public": False,
        }
