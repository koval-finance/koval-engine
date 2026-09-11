"""Evidence-ready OHLCV identity, validation, and canonical byte encoding."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

import numpy as np

from koval.engine.market_identity import resolve_market_identity
from koval.exchanges.base import OHLCV_COLUMNS, timeframe_ms

_MAGIC = b"KOVAL-CANDLES-V1\n"
_ROW = struct.Struct("<qddddd")
_COUNT = struct.Struct("<Q")


@dataclass(frozen=True)
class CandleDatasetMetadata:
    exchange: str
    market: str
    canonical_symbol: str
    timeframe: str
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: int | None
    actual_end_ms: int | None
    row_count: int
    gaps: tuple[tuple[int, int], ...]
    duplicate_timestamps: tuple[int, ...]
    source: str
    volume_status: str
    encoding_version: str = "koval_candles_f64le_v1"


@dataclass(frozen=True)
class CandleDataset:
    candles: np.ndarray
    metadata: CandleDatasetMetadata
    sha256: str

    def encoded_candles(self) -> bytes:
        return canonical_candle_bytes(self.candles)


def _validated_candles(candles: np.ndarray) -> np.ndarray:
    values = np.asarray(candles, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != len(OHLCV_COLUMNS):
        raise ValueError("canonical candles must have shape (N, 6)")
    if not np.isfinite(values).all():
        raise ValueError("canonical candles must contain only finite values")
    if len(values) and (
        np.any(values[:, 0] < 0) or np.any(values[:, 0] != values[:, 0].astype(np.int64))
    ):
        raise ValueError("canonical candle timestamps must be non-negative integers")
    if len(values):
        opens, highs, lows, closes, volumes = (values[:, index] for index in range(1, 6))
        if np.any(np.column_stack((opens, highs, lows, closes)) <= 0):
            raise ValueError("canonical candle prices must be positive")
        if np.any(volumes < 0):
            raise ValueError("canonical candle volume must be non-negative")
        if np.any(highs < np.maximum.reduce((opens, lows, closes))) or np.any(
            lows > np.minimum.reduce((opens, highs, closes))
        ):
            raise ValueError("canonical candle high/low bounds are invalid")
    return values


def canonical_candle_bytes(candles: np.ndarray) -> bytes:
    values = _validated_candles(candles)
    payload = bytearray(_MAGIC)
    payload.extend(_COUNT.pack(len(values)))
    for row in values:
        payload.extend(_ROW.pack(int(row[0]), *(float(value) for value in row[1:])))
    return bytes(payload)


def decode_canonical_candles(payload: bytes) -> np.ndarray:
    if not payload.startswith(_MAGIC) or len(payload) < len(_MAGIC) + _COUNT.size:
        raise ValueError("unsupported canonical candle encoding")
    offset = len(_MAGIC)
    (count,) = _COUNT.unpack_from(payload, offset)
    offset += _COUNT.size
    expected_size = offset + count * _ROW.size
    if len(payload) != expected_size:
        raise ValueError("canonical candle payload length does not match its row count")
    rows = [_ROW.unpack_from(payload, offset + index * _ROW.size) for index in range(count)]
    return np.asarray(rows, dtype=np.float64).reshape((count, len(OHLCV_COLUMNS)))


def build_candle_dataset(
    candles: np.ndarray,
    *,
    exchange: str,
    market: str,
    symbol: str,
    timeframe: str,
    requested_start_ms: int,
    requested_end_ms: int,
    source: str,
) -> CandleDataset:
    values = _validated_candles(candles).copy()
    step = timeframe_ms(timeframe)
    identity = resolve_market_identity(exchange=exchange, market=market, symbol=symbol)
    for bound in (requested_start_ms, requested_end_ms):
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0 or bound % step:
            raise ValueError(
                "candle request bounds must be non-negative aligned integer timestamps"
            )
    if requested_end_ms < requested_start_ms:
        raise ValueError("candle request end must not precede start")
    if not str(source).strip():
        raise ValueError("candle evidence source is required")
    timestamps = values[:, 0].astype(np.int64)
    unique, counts = np.unique(timestamps, return_counts=True)
    duplicates = tuple(int(value) for value in unique[counts > 1])
    if duplicates:
        raise ValueError(f"duplicate candle timestamp: {duplicates[0]}")
    if len(timestamps) > 1 and np.any(np.diff(timestamps) <= 0):
        raise ValueError("canonical candle timestamps must be strictly increasing")
    if (
        np.any(timestamps % step)
        or np.any(timestamps < requested_start_ms)
        or np.any(timestamps >= requested_end_ms)
    ):
        raise ValueError(
            "canonical candles must align and stay inside the requested half-open range"
        )
    expected = np.arange(requested_start_ms, requested_end_ms, step, dtype=np.int64)
    missing = np.setdiff1d(expected, timestamps, assume_unique=True)
    gaps = tuple((int(timestamp), int(timestamp + step)) for timestamp in missing)
    if gaps:
        raise ValueError(f"incomplete candle coverage: {gaps[0]}")
    # Back the public array with immutable bytes. Merely clearing NumPy's
    # WRITEABLE flag on an owning array can be reversed by a caller.
    values = np.frombuffer(values.tobytes(order="C"), dtype=np.float64).reshape(
        (-1, len(OHLCV_COLUMNS))
    )
    encoded = canonical_candle_bytes(values)
    metadata = CandleDatasetMetadata(
        exchange=identity.exchange,
        market=identity.market,
        canonical_symbol=identity.canonical_symbol,
        timeframe=timeframe,
        requested_start_ms=int(requested_start_ms),
        requested_end_ms=int(requested_end_ms),
        actual_start_ms=int(timestamps[0]) if len(timestamps) else None,
        actual_end_ms=int(timestamps[-1] + step) if len(timestamps) else None,
        row_count=len(values),
        gaps=gaps,
        duplicate_timestamps=duplicates,
        source=str(source),
        volume_status="observed",
    )
    return CandleDataset(values, metadata, hashlib.sha256(encoded).hexdigest())


__all__ = [
    "CandleDataset",
    "CandleDatasetMetadata",
    "build_candle_dataset",
    "canonical_candle_bytes",
    "decode_canonical_candles",
]
