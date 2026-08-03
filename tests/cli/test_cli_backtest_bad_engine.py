"""A misconfigured engine override must explain itself, not dump a traceback.

`KOVAL_BACKTEST_ENGINE` is a module path typed by a human, so it will be typed
wrongly. The engine loader imports it directly, which raises ImportError (or
AttributeError, when the module exists but exposes no `create_engine`).
"""

from koval.cli import main


def test_unimportable_engine_override_reports_cleanly(
    capsys, monkeypatch, sample_csv, example_graph
):
    monkeypatch.setenv("KOVAL_BACKTEST_ENGINE", "no.such.engine.module")

    exit_code = main(["backtest", example_graph, "--data", sample_csv, "--timeframe", "1h"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no.such.engine.module" in captured.err
    assert "KOVAL_BACKTEST_ENGINE" in captured.err
    assert "Traceback" not in captured.err


def test_engine_module_without_a_factory_reports_cleanly(
    capsys, monkeypatch, sample_csv, example_graph
):
    # `json` imports fine but has no `create_engine` attribute.
    monkeypatch.setenv("KOVAL_BACKTEST_ENGINE", "json")

    exit_code = main(["backtest", example_graph, "--data", sample_csv, "--timeframe", "1h"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "create_engine" in captured.err
    assert "Traceback" not in captured.err
