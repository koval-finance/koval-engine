"""The installed distribution is the single source of the version string."""

from __future__ import annotations

from importlib import metadata

import koval


def test_package_exposes_the_version_as_a_string():
    assert isinstance(koval.__version__, str)


def test_version_matches_installed_distribution_metadata():
    assert koval.__version__ == metadata.version("koval-engine")


def test_version_is_a_semver_triplet():
    parts = koval.__version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
