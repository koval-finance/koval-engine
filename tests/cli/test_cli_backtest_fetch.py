from unittest.mock import MagicMock, patch

import numpy as np

from koval.cli import main


def test_fetch_path_requests_the_requested_window(capsys, example_graph):
    candles = np.array(
        [[1_700_000_000_000 + i * 3_600_000, 10, 11, 9, 10.5, 100.0] for i in range(5)]
    )
    cache = MagicMock()
    cache.get.return_value = candles
    engine = MagicMock()
    engine.run.return_value = MagicMock(metrics={"total_trades": 0}, trades=[])

    with (
        patch("koval.cli.main.OhlcvCache", return_value=cache),
        patch("koval.engine.backtest_engine.load_backtest_engine", return_value=engine),
    ):
        exit_code = main(
            [
                "backtest",
                example_graph,
                "--symbol",
                "BTCUSDT",
                "--timeframe",
                "1h",
                "--from",
                "2024-01-01",
                "--to",
                "2024-02-01",
            ]
        )

    assert exit_code == 0
    kwargs = cache.get.call_args.kwargs
    assert kwargs["symbol"] == "BTCUSDT"
    assert kwargs["timeframe"] == "1h"
    assert kwargs["start_ms"] < kwargs["end_ms"]


def test_symbol_and_data_are_mutually_exclusive(capsys, example_graph, sample_csv):
    exit_code = main(
        [
            "backtest",
            example_graph,
            "--symbol",
            "BTCUSDT",
            "--data",
            sample_csv,
            "--timeframe",
            "1h",
        ]
    )

    assert exit_code == 1
    assert "either" in capsys.readouterr().err.lower()


def test_neither_symbol_nor_data_is_an_error(capsys, example_graph):
    exit_code = main(["backtest", example_graph, "--timeframe", "1h"])

    assert exit_code == 1
    assert "either" in capsys.readouterr().err.lower()


def test_symbol_without_a_date_range_is_an_error(capsys, example_graph):
    exit_code = main(["backtest", example_graph, "--symbol", "BTCUSDT", "--timeframe", "1h"])

    assert exit_code == 1
    assert "--from" in capsys.readouterr().err


def test_empty_fetch_result_reports_cleanly(capsys, example_graph):
    cache = MagicMock()
    cache.get.return_value = np.empty((0, 6))

    with patch("koval.cli.main.OhlcvCache", return_value=cache):
        exit_code = main(
            [
                "backtest",
                example_graph,
                "--symbol",
                "BTCUSDT",
                "--timeframe",
                "1h",
                "--from",
                "2024-01-01",
                "--to",
                "2024-02-01",
            ]
        )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no candles" in captured.err.lower()
    assert "Traceback" not in captured.err


def test_exchange_type_flag_threads_into_fetch_and_execution_config(example_graph):
    candles = np.array(
        [[1_700_000_000_000 + i * 3_600_000, 10, 11, 9, 10.5, 100.0] for i in range(5)]
    )
    cache = MagicMock()
    cache.get.return_value = candles
    engine = MagicMock()
    engine.run.return_value = MagicMock(metrics={"total_trades": 0}, trades=[])

    with (
        patch("koval.cli.main.OhlcvCache", return_value=cache),
        patch("koval.engine.backtest_engine.load_backtest_engine", return_value=engine),
    ):
        exit_code = main(
            [
                "backtest",
                example_graph,
                "--exchange",
                "binance",
                "--exchange-type",
                "spot",
                "--symbol",
                "BTCUSDT",
                "--timeframe",
                "1h",
                "--from",
                "2024-01-01",
                "--to",
                "2024-02-01",
            ]
        )

    assert exit_code == 0
    assert cache.get.call_args.kwargs["exchange_type"] == "spot"
    spec = engine.run.call_args.args[0]
    assert spec.execution_config["exchange_type"] == "spot"
