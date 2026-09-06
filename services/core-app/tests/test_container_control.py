"""Unit tests for the container-runtime seam itself (#891) — selection, not mechanics.

Which runtime the core picks is the one decision an operator makes here, and getting it
wrong is silent: under Compose a wrong pick would break module removal, and in a pod it would
leave the core waiting on a daemon that does not exist. So the detection order and the
``none`` degradation are asserted directly, with the environment as the only input.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from epicurus_core_app import container_control
from epicurus_core_app.container_control import (
    PROTECTED,
    RESTARTABLE,
    ContainerControlError,
    ContainerController,
    resolve_runtime,
    select_controller,
)
from epicurus_core_app.docker_control import DockerController
from epicurus_core_app.kubernetes_control import KubernetesController


@pytest.fixture(autouse=True)
def _no_ambient_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Neutralize the *host's* environment: these assert the code's order, not this machine."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(container_control, "DOCKER_SOCKET", tmp_path / "absent.sock")


# ── resolve_runtime: the auto-detection order ────────────────────────────────────


def test_auto_picks_kubernetes_inside_a_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("DOCKER_HOST", "tcp://docker-proxy-core:2375")
    # Kubernetes wins even with DOCKER_HOST set: only a pod has the service host.
    assert resolve_runtime("auto") == "kubernetes"


def test_auto_picks_docker_from_docker_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://docker-proxy-core:2375")
    assert resolve_runtime("auto") == "docker"


def test_auto_picks_docker_from_a_mounted_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    socket = tmp_path / "docker.sock"
    socket.write_text("", encoding="utf-8")
    monkeypatch.setattr(container_control, "DOCKER_SOCKET", socket)
    assert resolve_runtime("auto") == "docker"


def test_auto_picks_none_with_no_runtime_at_all() -> None:
    assert resolve_runtime("auto") == "none"


@pytest.mark.parametrize("choice", ["docker", "kubernetes", "none"])
def test_an_explicit_choice_wins_over_detection(
    choice: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("DOCKER_HOST", "tcp://docker-proxy-core:2375")
    assert resolve_runtime(choice) == choice


def test_case_and_whitespace_are_forgiven() -> None:
    assert resolve_runtime("  Kubernetes  ") == "kubernetes"


def test_an_unknown_value_degrades_to_auto_rather_than_failing_startup() -> None:
    # Startup must never die on a typo'd env var; auto still finds nothing here.
    assert resolve_runtime("podman") == "none"


def test_a_blank_value_is_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://docker-proxy-core:2375")
    assert resolve_runtime("") == "docker"


# ── select_controller: what the app actually wires ───────────────────────────────


def test_none_yields_no_controller_and_an_explaining_reason() -> None:
    availability = select_controller("none")
    assert availability.controller is None
    assert availability.runtime == "none"
    assert availability.reason is not None
    assert "CONTAINER_RUNTIME=none" in availability.reason


def test_auto_with_nothing_present_says_why() -> None:
    availability = select_controller("auto")
    assert availability.controller is None
    assert "no container runtime detected" in (availability.reason or "")


def test_none_announces_itself_once_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unavailable line is a startup fact, not a per-call complaint (#891).

    ``select_controller`` runs once and emits exactly one line; because it hands back no
    controller, there is nothing left that *could* log again — a hundred removals produce the
    same single line the first one did.
    """
    events: list[str] = []

    class _Recorder:
        def info(self, event: str, **_: object) -> None:
            events.append(event)

        def warning(self, event: str, **_: object) -> None:
            events.append(event)

    monkeypatch.setattr(container_control, "log", _Recorder())
    availability = select_controller("none")
    assert availability.controller is None
    assert events == ["container control unavailable; container teardown deferred"]


def test_kubernetes_is_selected_without_a_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selection must not depend on the API server answering — it is probe-free."""
    monkeypatch.setenv("KUBERNETES_NAMESPACE", "epicurus")
    availability = select_controller("kubernetes")
    assert availability.runtime == "kubernetes"
    assert availability.controller is not None


def test_docker_selection_degrades_when_the_sdk_cannot_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import docker as docker_sdk

    def _boom() -> object:
        raise RuntimeError("permission denied while trying to connect to the Docker daemon")

    monkeypatch.setattr(docker_sdk, "from_env", _boom)
    availability = select_controller("docker")
    assert availability.runtime == "docker"
    assert availability.controller is None
    assert "permission denied" in (availability.reason or "")


# ── the policy the seam owns ─────────────────────────────────────────────────────


def test_both_arms_satisfy_the_protocol_and_enforce_the_same_policy() -> None:
    """A name protected under Docker is protected in a cluster, and vice versa (#891).

    The policy sets live on the seam so this cannot drift, but a shared constant only helps if
    both adapters actually consult it — so this asserts the *behaviour* through the protocol,
    which also type-checks that each adapter really satisfies :class:`ContainerController`.
    """

    class _NoContainers:
        def list(self, all: bool = False, filters: object = None) -> list[object]:
            return []

    class _DockerClient:
        containers = _NoContainers()

    def _empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    controllers: list[ContainerController] = [
        DockerController(_DockerClient(), project="epicurus"),
        KubernetesController(
            httpx.Client(
                transport=httpx.MockTransport(_empty),
                base_url="https://kubernetes.default.svc",
            ),
            namespace="epicurus",
            token_path=None,
        ),
    ]
    for controller in controllers:
        for name in ("core-app", "core", "web", "postgres", "ollama"):
            with pytest.raises(ContainerControlError, match="protected"):
                controller.remove_module(name)
        for name in ("tasks", "core-app", "postgres"):
            with pytest.raises(ContainerControlError, match="not restartable"):
                controller.restart_service(name)
        # The one allowlisted restart target: permitted, and a no-op with nothing matching.
        assert controller.restart_service("ollama") is False

    assert {"core-app", "web", "postgres", "ollama"} <= PROTECTED
    assert "ollama" in RESTARTABLE and len(RESTARTABLE) == 1
