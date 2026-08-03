"""The source distribution must contain the complete public test suite."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_SOURCE_FILES = {
    Path(".cursor/rules/koval-engine.mdc"),
    Path(".github/copilot-instructions.md"),
    Path(".github/workflows/release.yml"),
    Path(".gitignore"),
    Path("AGENTS.md"),
    Path("CHANGELOG.md"),
    Path("CLAUDE.md"),
    Path("CODE_OF_CONDUCT.md"),
    Path("CONTRIBUTING.md"),
    Path("GEMINI.md"),
    Path("LICENSE"),
    Path("MANIFEST.in"),
    Path("README.md"),
    Path("SECURITY.md"),
    Path("agents_docs/README.md"),
    Path("examples/generate_sample.py"),
    Path("examples/run_backtest.py"),
    Path("pyproject.toml"),
    Path("scripts/verify.sh"),
}


def _public_test_files() -> set[Path]:
    return {
        path.relative_to(ROOT)
        for path in (ROOT / "tests").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _local_only_names() -> list[str]:
    return [
        Path(line.strip().rstrip("/")).name
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_sdist_contains_every_public_test(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".coverage",
            ".git",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "*.egg-info",
            "*.pyc",
            "__pycache__",
            "build",
            "data_cache",
            "dist",
            *_local_only_names(),
        ),
    )
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        cwd=source,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    archives = list(tmp_path.glob("*.tar.gz"))
    assert len(archives) == 1

    unpacked = tmp_path / "unpacked"
    with tarfile.open(archives[0], "r:gz") as archive:
        members = {
            Path(*Path(member.name).parts[1:]) for member in archive.getmembers() if member.isfile()
        }
        archive.extractall(unpacked, filter="data")

    assert _public_test_files() <= members
    assert REQUIRED_SOURCE_FILES <= members
    assert not any("__pycache__" in path.parts or path.suffix == ".pyc" for path in members)

    roots = [path for path in unpacked.iterdir() if path.is_dir()]
    assert len(roots) == 1
    guards = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_public_language.py",
            "tests/test_public_surface.py",
            "tests/test_release_workflow.py",
            "-q",
        ],
        cwd=roots[0],
        check=False,
        capture_output=True,
        text=True,
    )
    assert guards.returncode == 0, guards.stdout + guards.stderr
