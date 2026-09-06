"""Unit tests for the Kubernetes arm of the container-runtime seam (#891).

The API server is a ``httpx.MockTransport``: every request is recorded and answered from a
table, so these assert the *contract the cluster sees* — the label selector, the `/scale`
subresource, the merge-patch bodies, the ServiceAccount bearer token — without a cluster and
without the Docker-shaped assumptions the Compose path carries.

The safety guards are asserted here in their own right, not by analogy to
``test_docker_control.py``: the protected denylist, the component-label re-check on every
matched workload, and the rule that a refusal by the API server (403) degrades to *deferred*
rather than failing the removal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from epicurus_core_app.container_control import (
    ContainerControlDeferred,
    ContainerControlError,
)
from epicurus_core_app.kubernetes_control import (
    COMPONENT_LABEL,
    PART_OF_LABEL,
    RESTART_ANNOTATION,
    KubernetesController,
)

NAMESPACE = "epicurus"


def _workload(name: str, component: str, *, part_of: str = "epicurus") -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {PART_OF_LABEL: part_of, COMPONENT_LABEL: component},
        }
    }


class _Api:
    """A stand-in API server: canned list replies, plus a log of every request made."""

    def __init__(self, listings: dict[str, list[dict[str, Any]]] | None = None) -> None:
        # kind -> the items its list call returns (absent kind ⇒ empty list)
        self.listings = listings or {}
        self.requests: list[httpx.Request] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status >= 400:
            return httpx.Response(self.status, json={"message": "forbidden"})
        if request.method == "GET":
            kind = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"items": self.listings.get(kind, [])})
        return httpx.Response(200, json={})

    @property
    def patches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "PATCH"]

    def body(self, request: httpx.Request) -> Any:
        return json.loads(request.content)


def _controller(api: _Api, *, token_path: Path | None = None) -> tuple[KubernetesController, _Api]:
    client = httpx.Client(
        transport=httpx.MockTransport(api.handler), base_url="https://kubernetes.default.svc"
    )
    return KubernetesController(client, namespace=NAMESPACE, token_path=token_path), api


# ── remove_module: scale the module's Deployment to zero ──────────────────────────


def test_remove_module_scales_the_matching_deployment_to_zero() -> None:
    api = _Api({"deployments": [_workload("epicurus-tasks", "tasks")]})
    ctrl, api = _controller(api)

    assert ctrl.remove_module("tasks") == 1

    listing, patch = api.requests
    assert listing.method == "GET"
    assert listing.url.path == f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments"
    # Addressed by label selector, never by a guessed object name.
    assert listing.url.params["labelSelector"] == (
        f"{PART_OF_LABEL}=epicurus,{COMPONENT_LABEL}=tasks"
    )
    assert patch.method == "PATCH"
    assert patch.url.path == (
        f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/epicurus-tasks/scale"
    )
    assert patch.headers["content-type"] == "application/merge-patch+json"
    assert api.body(patch) == {"spec": {"replicas": 0}}


def test_remove_module_with_no_workload_is_a_noop() -> None:
    ctrl, api = _controller(_Api())
    assert ctrl.remove_module("tasks") == 0
    assert not api.patches  # nothing was touched


def test_remove_module_scales_every_match() -> None:
    api = _Api(
        {"deployments": [_workload("epicurus-tasks", "tasks"), _workload("tasks-2", "tasks")]}
    )
    ctrl, api = _controller(api)
    assert ctrl.remove_module("tasks") == 2
    assert len(api.patches) == 2


def test_remove_protected_service_raises_and_calls_nothing() -> None:
    for name in ("core-app", "web", "postgres", "nats", "openbao", "ollama"):
        ctrl, api = _controller(_Api({"deployments": [_workload(name, name)]}))
        with pytest.raises(ContainerControlError, match="protected"):
            ctrl.remove_module(name)
        assert not api.requests  # refused before it ever reaches the API server


def test_remove_skips_a_workload_whose_component_label_disagrees() -> None:
    """Defence-in-depth: the selector is the server's filter, not our guard.

    An API server that answers a ``tasks`` selector with a ``postgres`` object (a mislabelled
    workload, or a selector that matched too widely) must not have it scaled down.
    """
    api = _Api({"deployments": [_workload("data-postgres", "postgres")]})
    ctrl, api = _controller(api)
    assert ctrl.remove_module("tasks") == 0
    assert not api.patches


def test_remove_skips_a_workload_with_no_name() -> None:
    api = _Api({"deployments": [{"metadata": {"labels": {COMPONENT_LABEL: "tasks"}}}]})
    ctrl, api = _controller(api)
    assert ctrl.remove_module("tasks") == 0
    assert not api.patches


# ── the degraded modes: RBAC and an unreachable API server ───────────────────────


def test_forbidden_surfaces_as_deferred_not_a_crash() -> None:
    """A 403 from RBAC is the ordinary degraded mode — the caller tombstones anyway."""
    api = _Api()
    api.status = 403
    ctrl, _ = _controller(api)
    with pytest.raises(ContainerControlDeferred, match="403"):
        ctrl.remove_module("tasks")


def test_a_transport_failure_surfaces_as_deferred() -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kubernetes.default.svc is unreachable")

    client = httpx.Client(
        transport=httpx.MockTransport(_boom), base_url="https://kubernetes.default.svc"
    )
    ctrl = KubernetesController(client, namespace=NAMESPACE, token_path=None)
    with pytest.raises(ContainerControlDeferred, match="unreachable"):
        ctrl.remove_module("tasks")


def test_deferred_is_a_container_control_error() -> None:
    """Callers that only care about "it didn't happen" keep working unchanged."""
    assert issubclass(ContainerControlDeferred, ContainerControlError)


# ── restart_service: a rollout restart of an allowlisted workload ────────────────


def test_restart_patches_the_statefulset_pod_template_annotation() -> None:
    api = _Api({"statefulsets": [_workload("epicurus-ollama", "ollama")]})
    ctrl, api = _controller(api)

    assert ctrl.restart_service("ollama") is True

    patch = api.patches[0]
    assert patch.url.path == f"/apis/apps/v1/namespaces/{NAMESPACE}/statefulsets/epicurus-ollama"
    annotations = api.body(patch)["spec"]["template"]["metadata"]["annotations"]
    assert RESTART_ANNOTATION in annotations
    assert annotations[RESTART_ANNOTATION].endswith("Z")  # an RFC3339 stamp, as kubectl writes


def test_restart_falls_back_to_a_deployment() -> None:
    api = _Api({"statefulsets": [], "deployments": [_workload("epicurus-ollama", "ollama")]})
    ctrl, api = _controller(api)

    assert ctrl.restart_service("ollama") is True

    patch = api.patches[0]
    assert patch.url.path == f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/epicurus-ollama"


def test_restart_stops_at_the_statefulset_and_never_double_patches() -> None:
    api = _Api(
        {
            "statefulsets": [_workload("epicurus-ollama", "ollama")],
            "deployments": [_workload("epicurus-ollama", "ollama")],
        }
    )
    ctrl, api = _controller(api)
    assert ctrl.restart_service("ollama") is True
    assert len(api.patches) == 1


def test_restart_non_allowlisted_raises() -> None:
    ctrl, api = _controller(_Api())
    for name in ("core-app", "tasks", "postgres", "web"):
        with pytest.raises(ContainerControlError, match="not restartable"):
            ctrl.restart_service(name)
    assert not api.requests


def test_restart_with_no_workload_is_false() -> None:
    ctrl, api = _controller(_Api())
    assert ctrl.restart_service("ollama") is False
    assert not api.patches


# ── credentials: the pod's ServiceAccount token, re-read per request ─────────────


def test_the_service_account_token_is_sent_and_re_read(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("first-token\n", encoding="utf-8")
    api = _Api({"deployments": [_workload("epicurus-tasks", "tasks")]})
    ctrl, api = _controller(api, token_path=token)

    ctrl.remove_module("tasks")
    assert api.requests[0].headers["authorization"] == "Bearer first-token"

    # Projected tokens rotate on disk; the next call must pick the new one up rather than
    # keep presenting an expired credential for the life of the process.
    token.write_text("rotated-token\n", encoding="utf-8")
    ctrl.remove_module("tasks")
    assert api.requests[-1].headers["authorization"] == "Bearer rotated-token"


def test_a_missing_token_file_is_not_fatal(tmp_path: Path) -> None:
    api = _Api()
    ctrl, api = _controller(api, token_path=tmp_path / "absent")
    assert ctrl.remove_module("tasks") == 0
    assert "authorization" not in api.requests[0].headers


# ── from_env: namespace resolution ───────────────────────────────────────────────


def test_from_env_prefers_the_configured_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBERNETES_NAMESPACE", "from-env")
    availability = KubernetesController.from_env(namespace="explicit")
    assert availability.controller is not None
    assert availability.runtime == "kubernetes"
    assert availability.reason is None
    assert isinstance(availability.controller, KubernetesController)
    assert availability.controller.namespace == "explicit"


def test_from_env_reads_the_namespace_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUBERNETES_NAMESPACE", "from-env")
    availability = KubernetesController.from_env()
    assert availability.controller is not None
    assert isinstance(availability.controller, KubernetesController)
    assert availability.controller.namespace == "from-env"


def test_from_env_without_a_namespace_defers_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No namespace anywhere ⇒ unavailable with a reason — never a guessed 'default'."""
    monkeypatch.delenv("KUBERNETES_NAMESPACE", raising=False)
    monkeypatch.setattr("epicurus_core_app.kubernetes_control.SERVICE_ACCOUNT_DIR", tmp_path)
    availability = KubernetesController.from_env()
    assert availability.controller is None
    assert availability.reason is not None
    assert "namespace" in availability.reason
    assert availability.runtime == "kubernetes"


def test_from_env_reads_the_namespace_from_the_service_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("KUBERNETES_NAMESPACE", raising=False)
    (tmp_path / "namespace").write_text("epicurus-prod\n", encoding="utf-8")
    monkeypatch.setattr("epicurus_core_app.kubernetes_control.SERVICE_ACCOUNT_DIR", tmp_path)
    availability = KubernetesController.from_env()
    assert availability.controller is not None
    assert isinstance(availability.controller, KubernetesController)
    assert availability.controller.namespace == "epicurus-prod"
