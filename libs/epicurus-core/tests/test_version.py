"""Smoke tests that validate the package imports and the toolchain is wired up."""

from __future__ import annotations

import re
from importlib.metadata import version as dist_version

import epicurus_core


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", epicurus_core.__version__) is not None


def test_version_matches_the_packaging_metadata() -> None:
    """One number, two files — ``_version.py`` (what ``/health`` and traces report) and
    ``pyproject.toml`` (what gets published). They silently drifted a whole release apart
    because the check above only looks at the *shape*. Bump both."""
    assert epicurus_core.__version__ == dist_version("epicurus-core")


def test_version_is_exported() -> None:
    assert "__version__" in epicurus_core.__all__
