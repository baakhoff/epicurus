"""The per-test timeout safety net (#401): a deadlocked test must fail loudly, not hang.

Two things are proven here, neither of which needs Docker:

* a deliberately-hanging test, run under the timeout, aborts with a ``Timeout`` traceback
  instead of running forever (the #401 acceptance criterion);
* the repo-root ``conftest.py`` hook lifts the budget for ``integration``-marked tests so a
  cold testcontainers image pull never false-trips the 60s default.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_hang_trips_the_timeout(pytester: pytest.Pytester) -> None:
    """A test that never returns is killed by the deadline with a dumped stack."""
    pytester.makepyfile(
        """
        import time


        def test_deadlock():
            # Stands in for a real async deadlock; the timer thread aborts it.
            time.sleep(30)
        """
    )
    # `thread` is the portable method (matches the repo default); on expiry it dumps the
    # stack and aborts the process, so we assert on the exit code + banner, not on a tidy
    # pytest summary (which os._exit skips).
    result = pytester.runpytest_subprocess("--timeout=1", "--timeout-method=thread")
    assert result.ret != 0, "a hung test must make the run fail"
    combined = result.stdout.str() + result.stderr.str()
    assert "Timeout" in combined, "the timeout banner/traceback must be reported"
    assert "test_deadlock" in combined, "the offending test must be named in the dump"


def _load_root_conftest() -> ModuleType:
    """Load the repo-root ``conftest.py`` by path so its hook can be unit-tested directly."""
    spec = importlib.util.spec_from_file_location("_root_conftest", REPO / "conftest.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeItem:
    """Minimal pytest.Item stand-in: tracks markers and any added during the hook."""

    def __init__(self, *marker_names: str) -> None:
        self._markers = {name: SimpleNamespace(name=name) for name in marker_names}
        self.added: list[pytest.MarkDecorator] = []

    def get_closest_marker(self, name: str) -> object | None:
        for marker in self.added:
            if marker.name == name:
                return marker
        return self._markers.get(name)

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.added.append(marker)


def test_integration_marker_lifts_timeout() -> None:
    """The hook gives integration tests a generous budget, leaving everything else alone."""
    conftest = _load_root_conftest()
    integration = _FakeItem("integration")
    unit = _FakeItem()
    own_timeout = _FakeItem("integration", "timeout")  # already carries its own @timeout

    conftest.pytest_collection_modifyitems(
        config=None,  # type: ignore[arg-type]  # the hook ignores config
        items=[integration, unit, own_timeout],  # type: ignore[list-item]
    )

    # An integration test with no explicit timeout gets the lifted ceiling …
    assert len(integration.added) == 1
    assert integration.added[0].name == "timeout"
    assert integration.added[0].args == (300,)
    # … a plain unit test keeps the tight global default …
    assert unit.added == []
    # … and an integration test with its own @timeout is never overridden.
    assert own_timeout.added == []
