"""Shared fixtures for the repo-level tests.

``scripts/*.py`` are standalone tools, not workspace packages, so we load them
by path rather than importing them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def new_module() -> ModuleType:
    """The ``scripts/new_module.py`` module, loaded by path."""
    return _load("new_module")


@pytest.fixture
def check_docs_links() -> ModuleType:
    """The ``scripts/check_docs_links.py`` module, loaded by path."""
    return _load("check_docs_links")
