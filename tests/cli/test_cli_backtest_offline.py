import json

import pytest

from koval.cli import main

pytestmark = pytest.mark.backtrader  # a real run needs the GPL adapter


def test_backtest_prints_metrics_for_a_local_csv(capsys, sample_csv, example_graph):
    exit_code = main(["backtest", example_graph, "--data", sample_csv, "--timeframe", "1h"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "total_trades" in out


def test_backtest_json_output_is_machine_readable(capsys, sample_csv, example_graph):
    exit_code = main(
        ["backtest", example_graph, "--data", sample_csv, "--timeframe", "1h", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert "metrics" in payload and "trades" in payload
