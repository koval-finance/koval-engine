"""Package version, read from installed distribution metadata."""

from __future__ import annotations

from importlib import metadata

__version__ = metadata.version("koval-engine")
