"""The bundled examples must be reachable after `pip install`, with no checkout.

The README quickstart tells a pip user to validate and backtest an example. If
the examples live only in the git repository, that quickstart is a lie for
everyone who installed from PyPI.
"""

import json

from koval.cli import main
from koval.examples import EXAMPLES_DIR, available_graphs, example_path


def test_examples_ship_inside_the_installed_package():
    assert EXAMPLES_DIR.is_dir()
    assert (EXAMPLES_DIR / "graphs" / "ema_cross_trend.json").is_file()
    assert (EXAMPLES_DIR / "data" / "sample-1h.csv").is_file()


def test_example_path_resolves_a_bundled_file():
    path = example_path("graphs", "ema_cross_trend.json")

    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "ema_cross_trend"


def test_available_graphs_lists_the_bundled_graphs():
    assert "ema_cross_trend" in available_graphs()


def test_examples_command_prints_the_bundled_directory(capsys):
    exit_code = main(["examples"])
    out = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert out == str(EXAMPLES_DIR)


def test_examples_copy_writes_a_usable_tree(tmp_path, capsys):
    exit_code = main(["examples", "--copy", str(tmp_path)])
    out = capsys.readouterr().out

    assert exit_code == 0
    destination = tmp_path / "koval-examples"
    assert (destination / "graphs" / "ema_cross_trend.json").is_file()
    assert (destination / "data" / "sample-1h.csv").is_file()
    assert str(destination) in out


def test_copied_example_validates(tmp_path):
    main(["examples", "--copy", str(tmp_path)])
    graph = tmp_path / "koval-examples" / "graphs" / "ema_cross_trend.json"

    assert main(["validate", str(graph)]) == 0


def test_examples_copy_refuses_to_overwrite_silently(tmp_path, capsys):
    assert main(["examples", "--copy", str(tmp_path)]) == 0
    exit_code = main(["examples", "--copy", str(tmp_path)])

    assert exit_code == 1
    assert "exists" in capsys.readouterr().err.lower()
