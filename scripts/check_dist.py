#!/usr/bin/env python3
"""Fail when a built distribution does not carry this tree's engine sources.

Run after `python -m build`, before any upload, and whenever a sibling
repository is about to install from a local artifact. A stored result records
an engine version and nothing else, so that version identifies the matching
rules that produced it only while one version means one build.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "koval"
MAX_REPORTED = 10


def tree_version() -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


def tree_payload(prefix: str) -> dict[str, bytes]:
    return {
        prefix + path.relative_to(PACKAGE).as_posix(): path.read_bytes()
        for path in PACKAGE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def wheel_payload(artifact: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(artifact) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
            if name.startswith("koval/") and not name.endswith("/")
        }


def sdist_payload(artifact: Path) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    with tarfile.open(artifact, "r:gz") as archive:
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if not member.isfile() or parts[1:3] != ("src", "koval"):
                continue
            extracted = archive.extractfile(member)
            if extracted is not None:
                payload["/".join(parts[1:])] = extracted.read()
    return payload


def artifact_version(artifact: Path) -> str | None:
    """The version an artifact's name claims, or None when the name carries none."""
    name = artifact.name
    parts = name.split("-") if name.endswith(".whl") else name[: -len(".tar.gz")].split("-")
    if len(parts) < 2:
        return None
    return parts[1] if name.endswith(".whl") else parts[-1]


def compare(artifact: Path) -> list[str]:
    """Report every way this artifact disagrees with the working tree."""
    if artifact.name.endswith(".whl"):
        packaged, expected = wheel_payload(artifact), tree_payload("koval/")
    else:
        packaged, expected = sdist_payload(artifact), tree_payload("src/koval/")

    problems = []
    version = artifact_version(artifact)
    if version is None:
        problems.append(f"carries no version in its name, tree is at {tree_version()}")
    elif version != tree_version():
        problems.append(f"declares version {version}, tree is at {tree_version()}")

    missing = sorted(set(expected) - set(packaged))
    unexpected = sorted(set(packaged) - set(expected))
    changed = sorted(
        name for name in set(packaged) & set(expected) if packaged[name] != expected[name]
    )
    for label, names in (("missing", missing), ("unexpected", unexpected), ("changed", changed)):
        for name in names[:MAX_REPORTED]:
            problems.append(f"{label}: {name}")
        if len(names) > MAX_REPORTED:
            problems.append(f"{label}: ... and {len(names) - MAX_REPORTED} more")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=ROOT / "dist")
    parser.add_argument("--require-artifacts", action="store_true")
    args = parser.parse_args(argv[1:])
    directory = args.directory
    artifacts = sorted(
        path
        for path in directory.glob("*")
        if path.name.endswith(".whl") or path.name.endswith(".tar.gz")
    )
    if not artifacts:
        print(f"no distributions found in {directory}")
        return 1 if args.require_artifacts else 0

    failed = False
    for artifact in artifacts:
        problems = compare(artifact)
        if not problems:
            print(f"{artifact.name}: matches src/koval at {tree_version()}")
            continue
        failed = True
        print(f"{artifact.name}: does not carry this tree's engine sources")
        for problem in problems:
            print(f"  {problem}")
    if failed:
        print(
            "\nA distribution must be the code it names. Delete the stale artifacts "
            "and rebuild before uploading or installing from this directory."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
