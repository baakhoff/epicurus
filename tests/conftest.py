"""Shared fixtures for the repo-level tests.

``scripts/new_module.py`` is a standalone tool, not a workspace package, so we
load it by path rather than importing it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def new_module() -> ModuleType:
    """The ``scripts/new_module.py`` module, loaded by path."""
    spec = importlib.util.spec_from_file_location("new_module", REPO / "scripts" / "new_module.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
