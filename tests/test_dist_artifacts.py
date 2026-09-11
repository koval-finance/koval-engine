"""A built distribution must carry exactly this tree's engine sources.

The published version string is the only handle a stored result has on the
matching rules that produced it, so an artifact that disagrees with the tree
it claims to come from cannot be allowed near an upload or a sibling venv.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "check_dist.py"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _run_gate(dist_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), str(dist_dir)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory) -> Path:
    outdir = tmp_path_factory.mktemp("dist")
    build = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(outdir), str(ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return outdir


def _only(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    assert len(matches) == 1, f"expected one {pattern} in {directory}, found {matches}"
    return matches[0]


def _repacked_wheel(wheel: Path, destination: Path, member: str, content: bytes) -> Path:
    target = destination / wheel.name
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(target, "w") as repacked:
        for info in source.infolist():
            payload = content if info.filename == member else source.read(info.filename)
            repacked.writestr(info, payload)
    return target


def test_gate_accepts_a_freshly_built_wheel_and_sdist(built_distributions):
    result = _run_gate(built_distributions)
    assert result.returncode == 0, result.stdout + result.stderr


def test_gate_rejects_a_wheel_whose_engine_source_differs(built_distributions, tmp_path):
    original = _only(built_distributions, "*.whl")
    with zipfile.ZipFile(original) as archive:
        mutated = archive.read("koval/engine/paper_broker.py") + b"\n# published-only behavior\n"
    _repacked_wheel(original, tmp_path, "koval/engine/paper_broker.py", mutated)

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "koval/engine/paper_broker.py" in result.stdout


def test_gate_rejects_a_wheel_missing_an_engine_module(built_distributions, tmp_path):
    original = _only(built_distributions, "*.whl")
    target = tmp_path / original.name
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(target, "w") as repacked:
        for info in source.infolist():
            if info.filename == "koval/engine/funding.py":
                continue
            repacked.writestr(info, source.read(info.filename))

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "koval/engine/funding.py" in result.stdout


def test_gate_rejects_an_artifact_whose_version_is_not_the_tree_version(
    built_distributions, tmp_path
):
    original = _only(built_distributions, "*.whl")
    stale = tmp_path / original.name.replace(VERSION, "0.0.1")
    shutil.copyfile(original, stale)

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "0.0.1" in result.stdout
    assert VERSION in result.stdout


def test_gate_reports_an_artifact_whose_filename_carries_no_version(built_distributions, tmp_path):
    shutil.copyfile(_only(built_distributions, "*.whl"), tmp_path / "mystery.whl")

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "mystery.whl" in result.stdout
    assert "Traceback" not in result.stderr


def test_gate_rejects_an_sdist_whose_engine_source_differs(built_distributions, tmp_path):
    original = _only(built_distributions, "*.tar.gz")
    target = tmp_path / original.name
    member_name = f"koval_engine-{VERSION}/src/koval/engine/paper_profile.py"
    with tarfile.open(original, "r:gz") as source, tarfile.open(target, "w:gz") as repacked:
        for info in source.getmembers():
            extracted = source.extractfile(info)
            payload = extracted.read() if extracted is not None else b""
            if info.name == member_name:
                payload = payload + b"\n# published-only behavior\n"
                info.size = len(payload)
            repacked.addfile(info, io.BytesIO(payload) if info.isfile() else None)

    result = _run_gate(tmp_path)

    assert result.returncode == 1
    assert "src/koval/engine/paper_profile.py" in result.stdout


def test_gate_reports_nothing_to_check_for_an_empty_directory(tmp_path):
    result = _run_gate(tmp_path)
    assert result.returncode == 0


def test_repository_dist_directory_holds_no_artifact_that_disagrees_with_the_tree():
    dist = ROOT / "dist"
    if not dist.is_dir():
        pytest.skip("no local dist/ directory")
    result = _run_gate(dist)
    assert result.returncode == 0, result.stdout + result.stderr
