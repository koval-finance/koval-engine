"""Packaging guards for the standalone koval-engine distribution."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_RUNTIME_DEPS = ("pymongo", "motor", "mongomock", "fastapi", "uvicorn", "backtrader")


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_koval_package_ships_py_typed_marker():
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]
    assert "py.typed" in package_data["koval"]
    assert (ROOT / "src" / "koval" / "py.typed").is_file()


def test_runtime_dependencies_exclude_database_and_web_framework():
    dependencies = _pyproject()["project"]["dependencies"]
    offenders = [dep for dep in dependencies if dep.lower().startswith(FORBIDDEN_RUNTIME_DEPS)]
    assert offenders == [], f"engine must not depend on: {offenders}"


def test_console_script_is_declared():
    assert _pyproject()["project"]["scripts"]["koval"] == "koval.cli:main"
