"""Runnable examples shipped inside the installed package.

These files travel with the wheel, so `pip install koval-engine` is enough to
run the documented quickstart — no git checkout required. Use
:func:`example_path` from Python, or ``koval examples`` from the command line.
"""

from __future__ import annotations

from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent


def example_path(*parts: str) -> Path:
    """Return the path to a bundled example file, e.g. ``("graphs", "x.json")``."""
    return EXAMPLES_DIR.joinpath(*parts)


def available_graphs() -> list[str]:
    """Names of the bundled strategy graphs, without the ``.json`` suffix."""
    return sorted(path.stem for path in (EXAMPLES_DIR / "graphs").glob("*.json"))


__all__ = ["EXAMPLES_DIR", "available_graphs", "example_path"]
