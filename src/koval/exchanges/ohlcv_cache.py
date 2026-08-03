"""Parquet-backed OHLCV cache.

One file per ``(exchange, symbol, timeframe)``. Append-only for closed candles.
Read path serves from disk; on miss, asks the adapter only for the missing
slice, merges, persists, returns.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import portalocker

from koval.exchanges.base import OHLCV_COLUMNS, ExchangeAdapter, timeframe_ms

_LOCK_TIMEOUT_SECONDS = 30
_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")


class OhlcvContinuityError(RuntimeError):
    """Raised when an exchange response cannot repair a cached candle gap."""


def _symbol_to_filename(symbol: str) -> str:
    return symbol.replace("/", "").replace("_", "")


def _validate_path_component(value: str, *, field: str) -> str:
    if not _SAFE_PATH_COMPONENT.fullmatch(value) or ".." in value:
        raise ValueError(f"unsafe cache {field}: {value!r}")
    return value


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def _forming_bar_open_ms(timeframe: str, now_ms: int) -> int:
    step = timeframe_ms(timeframe)
    return (now_ms // step) * step


class OhlcvCache:
    def __init__(
        self,
        root: Path,
        *,
        now_ms: Callable[[], int] = _default_now_ms,
    ) -> None:
        self._root = Path(root)
        self._now_ms = now_ms

    def get(
        self,
        adapter: ExchangeAdapter,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> np.ndarray:
        """Return candles for ``[start_ms, end_ms)``, populating the cache.

        Invariant: the on-disk Parquet file is **append-only, contiguous, and
        closed-bars-only**. The currently-forming bar (the slot whose open is
        ``(now_ms // step) * step``) is always re-fetched on every request that
        includes it and is never persisted. Closed-side gaps extending past the
        cached envelope are filled up to the cache boundary so no internal hole
        can form.

        Concurrency: the entire read-modify-write cycle is guarded by a
        per-file advisory lock (``portalocker``) so concurrent callers from
        different threads or processes serialize on the same file.
        """
        timeframe_ms(timeframe)
        safe_exchange = _validate_path_component(exchange, field="exchange")
        safe_symbol = _validate_path_component(
            _symbol_to_filename(symbol),
            field="symbol",
        )
        path = self._path_for(safe_exchange, safe_symbol, timeframe)
        if not path.resolve().is_relative_to(self._root.resolve()):
            raise ValueError("cache path escapes its configured root")
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with portalocker.Lock(str(lock_path), timeout=_LOCK_TIMEOUT_SECONDS):
            return self._get_locked(
                adapter,
                path=path,
                symbol=symbol,
                timeframe=timeframe,
                start_ms=start_ms,
                end_ms=end_ms,
            )

    def _get_locked(
        self,
        adapter: ExchangeAdapter,
        *,
        path: Path,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> np.ndarray:
        forming_open = _forming_bar_open_ms(timeframe, self._now_ms())
        step = timeframe_ms(timeframe)
        cached = self._read(path)
        normalized = self._sort_dedupe(cached)
        dirty = not np.array_equal(cached, normalized)
        cached = normalized
        if cached.size and cached[-1, 0] >= forming_open:
            cached = cached[cached[:, 0] < forming_open]
            dirty = True

        closed_end = min(end_ms, forming_open)
        gaps: list[tuple[int, int]] = []
        if cached.size == 0:
            if start_ms < closed_end:
                gaps.append((start_ms, closed_end))
        else:
            cached_min = int(cached[0, 0])
            cached_max_excl = int(cached[-1, 0]) + step
            gaps.extend(self._internal_gaps(cached, step))
            if start_ms < cached_min:
                gaps.append((start_ms, cached_min))
            if closed_end > cached_max_excl:
                gaps.append((cached_max_excl, closed_end))

        chunks: list[np.ndarray] = []
        if cached.size:
            chunks.append(cached)
        for gap_start, gap_end in gaps:
            if gap_start >= gap_end:
                continue
            chunk = adapter.fetch_ohlcv(symbol, timeframe, gap_start, gap_end)
            if chunk.size:
                chunks.append(chunk)
                dirty = True

        if chunks:
            merged_closed = self._sort_dedupe(np.vstack(chunks))
            merged_closed = merged_closed[merged_closed[:, 0] < forming_open]
            unresolved_gaps = self._internal_gaps(merged_closed, step)
            if unresolved_gaps:
                raise OhlcvContinuityError(f"unresolved OHLCV continuity gap: {unresolved_gaps[0]}")
            if dirty:
                self._write(path, merged_closed)
        else:
            merged_closed = np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)

        live_tail = np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
        if end_ms > forming_open:
            live_tail = adapter.fetch_ohlcv(symbol, timeframe, forming_open, end_ms)

        full = np.vstack([merged_closed, live_tail]) if live_tail.size else merged_closed
        return self._slice(full, start_ms, end_ms)

    @staticmethod
    def _sort_dedupe(candles: np.ndarray) -> np.ndarray:
        if candles.size == 0:
            return candles
        order = np.argsort(candles[:, 0], kind="mergesort")
        candles = candles[order]
        _, unique_idx = np.unique(candles[:, 0], return_index=True)
        return candles[np.sort(unique_idx)]

    @staticmethod
    def _internal_gaps(candles: np.ndarray, step: int) -> list[tuple[int, int]]:
        if len(candles) < 2:
            return []
        timestamps = candles[:, 0].astype(np.int64)
        return [
            (int(previous) + step, int(current))
            for previous, current in zip(timestamps[:-1], timestamps[1:], strict=True)
            if current - previous > step
        ]

    # ---- internals -------------------------------------------------------

    def _path_for(self, exchange: str, symbol: str, timeframe: str) -> Path:
        return self._root / exchange / f"{_symbol_to_filename(symbol)}_{timeframe}.parquet"

    def _read(self, path: Path) -> np.ndarray:
        if not path.is_file():
            return np.empty((0, len(OHLCV_COLUMNS)), dtype=np.float64)
        df = pd.read_parquet(path)
        return df[list(OHLCV_COLUMNS)].to_numpy(dtype=np.float64, copy=True)

    def _write(self, path: Path, candles: np.ndarray) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if candles.size == 0:
            return
        df = pd.DataFrame(candles, columns=list(OHLCV_COLUMNS))
        df["timestamp_ms"] = df["timestamp_ms"].astype("int64")
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            df.to_parquet(temporary_path, index=False)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _slice(candles: np.ndarray, start_ms: int, end_ms: int) -> np.ndarray:
        ts = candles[:, 0]
        mask = (ts >= start_ms) & (ts < end_ms)
        return candles[mask].copy()
