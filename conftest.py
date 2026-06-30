"""Repo-root pytest configuration shared by every suite (libs / services / tests).

Two concerns live here, both tied to the per-test timeout (``[tool.pytest.ini_options]``):

* **Integration tests get a larger budget.** The global 60s deadline is sized for unit
  tests (which finish in well under a second); an ``integration``-marked test boots
  testcontainers, and a *cold* image pull alone can exceed 60s. So we lift the budget for
  any ``integration`` test that does not already carry its own ``@pytest.mark.timeout`` —
  a true hang there still fails (just later), but a slow Docker pull never false-trips.
* **The ``pytester`` fixture is enabled** so ``tests/test_pytest_timeout.py`` can prove the
  deadline actually trips a hang. ``pytest_plugins`` is only honoured in the rootdir
  conftest, which this is.
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

# Generous ceiling for integration tests: enough for a cold testcontainers image pull plus
# the test body, while still catching a genuine deadlock instead of hanging the gate.
_INTEGRATION_TIMEOUT_SECONDS = 300


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Lift the per-test timeout for ``integration`` tests (cold container pulls are slow)."""
    for item in items:
        if item.get_closest_marker("integration") and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(_INTEGRATION_TIMEOUT_SECONDS))
