import numpy as np
import pytest

from koval.cli.data import load_csv_candles

HEADER = "timestamp_ms,open,high,low,close,volume"


def _write(tmp_path, body):
    path = tmp_path / "candles.csv"
    path.write_text(f"{HEADER}\n{body}", encoding="utf-8")
    return path


def test_loads_candles_in_ohlcv_column_order(tmp_path):
    candles = load_csv_candles(_write(tmp_path, "1000,10,12,9,11,100\n2000,11,13,10,12,200\n"))

    assert candles.shape == (2, 6)
    assert candles.dtype == np.float64
    np.testing.assert_array_equal(candles[0], [1000, 10, 12, 9, 11, 100])


def test_rejects_a_file_with_unexpected_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("time,price\n1000,10\n", encoding="utf-8")

    with pytest.raises(ValueError, match="timestamp_ms"):
        load_csv_candles(path)


def test_rejects_out_of_order_rows(tmp_path):
    with pytest.raises(ValueError, match="chronological"):
        load_csv_candles(_write(tmp_path, "2000,11,13,10,12,200\n1000,10,12,9,11,100\n"))


def test_rejects_duplicate_timestamps(tmp_path):
    with pytest.raises(ValueError, match="chronological"):
        load_csv_candles(_write(tmp_path, "1000,10,12,9,11,100\n1000,11,13,10,12,200\n"))


def test_ignores_extra_columns(tmp_path):
    path = tmp_path / "extra.csv"
    path.write_text(
        "timestamp_ms,open,high,low,close,volume,note\n1000,10,12,9,11,100,hello\n",
        encoding="utf-8",
    )

    candles = load_csv_candles(path)

    assert candles.shape == (1, 6)
