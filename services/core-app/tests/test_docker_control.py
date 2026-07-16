"""Unit tests for DockerController — the tightly-scoped module-removal interface (#127).

The Docker SDK is replaced by in-memory fakes: a container records stop/remove, and the
client emulates label filtering. These assert the safety guards (protected denylist,
Compose-project scoping, idempotence, error wrapping) without touching a real socket.
"""

from __future__ import annotations

from typing import Any

import pytest

from epicurus_core_app.docker_control import DockerAvailability, DockerController, DockerError

_SERVICE = "com.docker.compose.service"
_PROJECT = "com.docker.compose.project"


class _FakeContainer:
    def __init__(self, service: str, project: str = "epicurus") -> None:
        self.labels: dict[str, str] = {_SERVICE: service, _PROJECT: project}
        self.stopped = False
        self.removed = False
        self.restarted = False

    def stop(self, timeout: int = 10) -> None:
        self.stopped = True

    def remove(self, force: bool = False) -> None:
        self.removed = True

    def restart(self, timeout: int = 10) -> None:
        self.restarted = True


class _FakeContainers:
    """Emulates ``client.containers.list(filters={"label": [...]})`` label matching."""

    def __init__(self, containers: list[_FakeContainer]) -> None:
        self._containers = containers

    def list(
        self, all: bool = False, filters: dict[str, list[str]] | None = None
    ) -> list[_FakeContainer]:
        # NB: the Docker SDK names this kwarg ``all``; avoid the shadowed builtin below.
        wanted = (filters or {}).get("label", [])
        out: list[_FakeContainer] = []
        for c in self._containers:
            matched = True
            for label in wanted:
                key, _, value = label.partition("=")
                if c.labels.get(key) != value:
                    matched = False
                    break
            if matched:
                out.append(c)
        return out


class _FakeClient:
    def __init__(self, containers: list[_FakeContainer]) -> None:
        self.containers = _FakeContainers(containers)


def _controller(
    containers: list[_FakeContainer], project: str | None = "epicurus"
) -> DockerController:
    return DockerController(_FakeClient(containers), project=project)


def test_remove_module_stops_and_removes_matching() -> None:
    c = _FakeContainer("tasks")
    count = _controller([c]).remove_module("tasks")
    assert count == 1
    assert c.stopped and c.removed


def test_remove_module_scopes_to_own_project() -> None:
    mine = _FakeContainer("tasks", project="epicurus")
    other = _FakeContainer("tasks", project="epicurus-ci")  # a co-located stack
    count = _controller([mine, other], project="epicurus").remove_module("tasks")
    assert count == 1
    assert mine.removed
    assert not other.removed  # the other stack is never touched


def test_remove_module_with_no_container_is_noop() -> None:
    assert _controller([]).remove_module("tasks") == 0


def test_remove_protected_service_raises() -> None:
    for name in ("core-app", "web", "postgres", "nats", "openbao"):
        ctrl = _controller([_FakeContainer(name)])
        with pytest.raises(DockerError, match="protected"):
            ctrl.remove_module(name)


def test_remove_skips_protected_even_if_filter_matches() -> None:
    # Defence-in-depth: a client that ignores filters and returns a protected container
    # must not have it stopped/removed when a *different* (non-protected) name is requested.
    class _DumbContainers:
        def __init__(self, c: _FakeContainer) -> None:
            self._c = c

        def list(self, all: bool = False, filters: Any = None) -> list[_FakeContainer]:
            return [self._c]

    class _DumbClient:
        def __init__(self, c: _FakeContainer) -> None:
            self.containers = _DumbContainers(c)

    protected = _FakeContainer("postgres")
    ctrl = DockerController(_DumbClient(protected), project="epicurus")
    count = ctrl.remove_module("tasks")
    assert count == 0
    assert not protected.removed


def test_docker_failure_is_wrapped() -> None:
    class _BoomContainers:
        def list(self, all: bool = False, filters: Any = None) -> list[_FakeContainer]:
            raise RuntimeError("socket gone")

    class _BoomClient:
        containers = _BoomContainers()

    ctrl = DockerController(_BoomClient(), project="epicurus")
    with pytest.raises(DockerError, match="failed to remove"):
        ctrl.remove_module("tasks")


# ── restart_service: the non-destructive, allowlisted restart path (#307) ─────────


def test_restart_service_restarts_allowlisted() -> None:
    c = _FakeContainer("ollama")
    assert _controller([c]).restart_service("ollama") is True
    assert c.restarted and not c.removed  # restart never removes


def test_restart_service_scopes_to_own_project() -> None:
    mine = _FakeContainer("ollama", project="epicurus")
    other = _FakeContainer("ollama", project="other-stack")
    assert _controller([mine, other], project="epicurus").restart_service("ollama") is True
    assert mine.restarted and not other.restarted


def test_restart_non_allowlisted_raises() -> None:
    ctrl = _controller([_FakeContainer("core-app"), _FakeContainer("tasks")])
    # Only RESTARTABLE names are permitted — core/modules/data-plane are all refused.
    for name in ("core-app", "tasks", "postgres", "web"):
        with pytest.raises(DockerError, match="not restartable"):
            ctrl.restart_service(name)


def test_restart_service_with_no_container_is_false() -> None:
    assert _controller([]).restart_service("ollama") is False


# ── from_env: probing at startup (#622) — reason and controller are never both set ────


def test_from_env_reports_the_real_reason_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import docker as docker_sdk

    def _boom() -> Any:
        raise RuntimeError("permission denied while trying to connect to the Docker daemon")

    monkeypatch.setattr(docker_sdk, "from_env", _boom)
    result = DockerController.from_env()
    assert result.controller is None
    assert result.reason is not None
    assert "permission denied" in result.reason


def test_from_env_succeeds_when_the_socket_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import docker as docker_sdk

    class _FakeSdkContainers:
        def get(self, _id: str) -> Any:
            raise RuntimeError("no such container")  # forces the COMPOSE_PROJECT_NAME fallback

    class _FakeSdkClient:
        containers = _FakeSdkContainers()

    monkeypatch.setattr(docker_sdk, "from_env", lambda: _FakeSdkClient())
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "epicurus-test")
    result = DockerController.from_env()
    assert isinstance(result, DockerAvailability)
    assert result.controller is not None
    assert result.reason is None
