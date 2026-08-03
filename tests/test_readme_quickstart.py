"""The commands printed in the README must be the commands that work."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from koval.cli import main

ROOT = Path(__file__).resolve().parent.parent


def _commands_in(text: str) -> list[str]:
    """Extract `koval ...` commands, joining shell line-continuations first."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [c.strip() for c in re.findall(r"^\s*\$?\s*(koval .+)$", joined, flags=re.MULTILINE)]


def _readme_koval_commands() -> list[str]:
    return _commands_in((ROOT / "README.md").read_text(encoding="utf-8"))


def _quickstart_commands() -> list[str]:
    """The commands in the Quick start section, in the order they appear."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    return _commands_in(readme.split("## Quick start", 1)[1].split("\n## ", 1)[0])


def test_readme_has_a_quickstart_with_commands():
    assert _quickstart_commands(), "the Quick start section must show koval commands"


def test_quickstart_runs_end_to_end(tmp_path, monkeypatch, capsys):
    """Run the quickstart in an empty directory, as a pip user would."""
    monkeypatch.chdir(tmp_path)

    ran = []
    for command in _quickstart_commands():
        argv = shlex.split(command)[1:]
        assert main(argv) == 0, f"README quickstart command failed: {command}"
        ran.append(argv[0])
        capsys.readouterr()

    assert "examples" in ran, "the quickstart must obtain the examples before using them"
    assert "validate" in ran, "the quickstart must validate a graph"


def test_quickstart_does_not_require_an_external_backtest_plugin():
    commands = _quickstart_commands()
    assert all(not command.startswith("koval backtest") for command in commands)

    quickstart = (
        (ROOT / "README.md")
        .read_text(encoding="utf-8")
        .split("## Quick start", 1)[1]
        .split("\n## ", 1)[0]
    )
    assert "pip install koval-engine koval-backtrader" not in quickstart


def test_custom_plugin_documentation_distinguishes_name_from_module_override():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert 'load_backtest_engine("my-engine")' in readme
    assert "KOVAL_BACKTEST_ENGINE=my_package.engine" in readme
    assert "KOVAL_BACKTEST_ENGINE=my-engine" not in readme


@pytest.mark.parametrize("command", [c for c in _readme_koval_commands()])
def test_every_readme_command_parses(command, tmp_path, monkeypatch):
    """A command that argparse rejects would fail the reader immediately."""
    monkeypatch.chdir(tmp_path)
    argv = shlex.split(command)[1:]
    parser_error = False
    try:
        main(argv)
    except SystemExit:
        parser_error = True
    except Exception:
        parser_error = False
    assert not parser_error, f"README command has invalid arguments: {command}"
