"""Smoke tests that validate the package imports and the toolchain is wired up."""

from __future__ import annotations

import re

import epicurus_core


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", epicurus_core.__version__) is not None


def test_version_is_exported() -> None:
    assert "__version__" in epicurus_core.__all__
