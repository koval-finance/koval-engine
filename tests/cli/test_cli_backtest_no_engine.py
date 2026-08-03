"""The first thing a user hits after installing only ``koval-engine``."""

from unittest.mock import patch

from koval.cli import main
from koval.engine.backtest_engine import NoBacktestEngineError


def test_missing_engine_reports_a_plugin_hint_without_a_traceback(
    capsys, sample_csv, example_graph
):
    message = "install or register a compatible backtest engine plugin"
    with patch(
        "koval.engine.backtest_engine.load_backtest_engine",
        side_effect=NoBacktestEngineError(message),
    ):
        exit_code = main(["backtest", example_graph, "--data", sample_csv, "--timeframe", "1h"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_missing_data_file_reports_cleanly(capsys, example_graph):
    exit_code = main(["backtest", example_graph, "--data", "/nonexistent.csv", "--timeframe", "1h"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Traceback" not in captured.err
