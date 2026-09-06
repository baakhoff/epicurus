"""In-cluster container control (#891) — the Kubernetes side of the seam.

On Kubernetes there is no daemon to talk to and no container to delete: a module *is* a
Deployment, and the equivalent of "stop and remove its container" is scaling that Deployment
to zero (the tombstone in the core's own prefs is what keeps it gone — ADR-0056/#382 — so
nothing here needs to delete the object the chart owns and would recreate on the next
``helm upgrade``). The Ollama restart is a rollout restart: patch the pod template's
``kubectl.kubernetes.io/restartedAt`` annotation, exactly what ``kubectl rollout restart``
does, so the new pod re-reads the env file the KV-cache apply just wrote (#307).

The API is reached with the pod's own ServiceAccount — the token file (re-read per request,
because projected tokens rotate), the cluster CA, and ``https://kubernetes.default.svc`` —
over the ``httpx`` client already in the stack, so this adds **no new dependency** and no
Kubernetes client package.

Workloads are addressed by **label selector, never by name**:
``app.kubernetes.io/part-of=epicurus,app.kubernetes.io/component=<name>``. The chart (#890)
renders those labels and the scoped Role that permits precisely ``get``/``list``/``patch`` on
``deployments``, ``deployments/scale`` and ``statefulsets``. Same :data:`PROTECTED` denylist
and same "only a configured module" guard as the Docker path — plus a re-check of each
matched workload's own component label, so even a mislabelled object can't be scaled down
under a different module's name.

RBAC that says no (a 403), or an API server that cannot be reached, raises
:class:`ContainerControlDeferred`: the module is still tombstoned and hidden immediately, and
only the scale-down waits — never a crash, never a failed removal.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

import httpx

from epicurus_core import get_logger
from epicurus_core_app.container_control import (
    PROTECTED,
    RESTARTABLE,
    ContainerAvailability,
    ContainerControlDeferred,
    ContainerControlError,
)

log = get_logger("epicurus_core_app.kubernetes_control")

#: The in-cluster API server address — a fixed, cluster-provided DNS name.
API_SERVER = "https://kubernetes.default.svc"
#: Where the kubelet projects the pod's ServiceAccount credentials.
SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
PART_OF_LABEL = "app.kubernetes.io/part-of"
COMPONENT_LABEL = "app.kubernetes.io/component"
PART_OF_VALUE = "epicurus"
#: The annotation ``kubectl rollout restart`` stamps; patching it re-creates the pods.
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

_APPS_V1 = "/apis/apps/v1"
_MERGE_PATCH = "application/merge-patch+json"
_TIMEOUT = 10.0


class KubernetesController:
    """Scales a *module's own* Deployment to zero; rollout-restarts an allowlisted workload."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        namespace: str,
        token_path: Path | None = SERVICE_ACCOUNT_DIR / "token",
    ) -> None:
        self._client = client
        self._namespace = namespace
        self._token_path = token_path

    @property
    def namespace(self) -> str:
        """The namespace every call is scoped to — nothing outside it is ever addressed."""
        return self._namespace

    @classmethod
    def from_env(cls, *, namespace: str = "") -> ContainerAvailability:
        """Build the in-cluster controller from the pod's own ServiceAccount; never raises.

        The namespace comes from ``KUBERNETES_NAMESPACE`` (the chart sets it from the downward
        API) or, failing that, the ServiceAccount's own ``namespace`` file. With neither, there
        is nothing safe to address, so control is reported unavailable — deferring teardown,
        exactly as an unreachable Docker socket does, rather than guessing a namespace.
        """
        ns = (namespace or os.environ.get("KUBERNETES_NAMESPACE") or "").strip()
        if not ns:
            ns = _read(SERVICE_ACCOUNT_DIR / "namespace") or ""
        if not ns:
            reason = (
                "kubernetes namespace unknown (set KUBERNETES_NAMESPACE, or run with a "
                "projected service account)"
            )
            log.warning("kubernetes control unavailable; container teardown deferred", error=reason)
            return ContainerAvailability(controller=None, reason=reason, runtime="kubernetes")
        ca_cert = SERVICE_ACCOUNT_DIR / "ca.crt"
        verify: str | bool = str(ca_cert) if ca_cert.exists() else True
        client = httpx.Client(base_url=API_SERVER, verify=verify, timeout=_TIMEOUT)
        log.info("kubernetes control ready", namespace=ns, api=API_SERVER)
        return ContainerAvailability(controller=cls(client, namespace=ns), runtime="kubernetes")

    # ── the contract ────────────────────────────────────────────────────────────────

    def remove_module(self, name: str) -> int:
        """Scale *name*'s Deployment(s) to zero; return how many were scaled down.

        Raises :class:`ContainerControlError` for a protected name. A name with no matching
        Deployment is a no-op (returns 0) — the same idempotence the Docker path has, which is
        what lets the startup tombstone reconcile re-run safely.
        """
        if name in PROTECTED:
            raise ContainerControlError(f"{name!r} is protected and cannot be removed")
        scaled = 0
        for workload in self._workloads("deployments", name):
            workload_name = _name_of(workload)
            if workload_name is None or not self._owns(workload, name):
                continue
            self._patch(
                f"{_APPS_V1}/namespaces/{self._namespace}/deployments/{workload_name}/scale",
                {"spec": {"replicas": 0}},
                what=f"scale {name!r} to zero",
            )
            log.info("scaled module deployment to zero", module=name, deployment=workload_name)
            scaled += 1
        if not scaled:
            log.info("no deployment matched the module; nothing to scale down", module=name)
        return scaled

    def restart_service(self, name: str) -> bool:
        """Rollout-restart an allowlisted infra workload in this namespace; ``True`` if one was.

        StatefulSet first (Ollama holds a model volume, so that is how the chart runs it), then
        Deployment — the same object either way, patched on its pod template so the kubelet
        re-creates the pod and it re-reads its environment.
        """
        if name not in RESTARTABLE:
            raise ContainerControlError(f"{name!r} is not restartable")
        stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        patch = {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: stamp}}}}}
        for kind in ("statefulsets", "deployments"):
            restarted = 0
            for workload in self._workloads(kind, name):
                workload_name = _name_of(workload)
                if workload_name is None or not self._owns(workload, name):
                    continue
                self._patch(
                    f"{_APPS_V1}/namespaces/{self._namespace}/{kind}/{workload_name}",
                    patch,
                    what=f"restart {name!r}",
                )
                log.info("rollout-restarted workload", service=name, kind=kind, name=workload_name)
                restarted += 1
            if restarted:
                return True
        log.info("no workload matched the service; nothing to restart", service=name)
        return False

    # ── API plumbing ────────────────────────────────────────────────────────────────

    def _workloads(self, kind: str, component: str) -> list[dict[str, Any]]:
        """Every ``kind`` object carrying this project's labels for *component*."""
        selector = f"{PART_OF_LABEL}={PART_OF_VALUE},{COMPONENT_LABEL}={component}"
        body = self._request(
            "GET",
            f"{_APPS_V1}/namespaces/{self._namespace}/{kind}",
            params={"labelSelector": selector},
            what=f"list {kind} for {component!r}",
        )
        items = body.get("items") if isinstance(body, dict) else None
        return [item for item in items or [] if isinstance(item, dict)]

    def _owns(self, workload: dict[str, Any], name: str) -> bool:
        """Defence-in-depth: the matched object must itself claim this exact name.

        The selector already narrows to it, but a server-side filter is not a guard — this is
        the Kubernetes twin of the Docker path re-reading each container's service label. It
        deliberately does not re-check :data:`PROTECTED` here: an *exact* name match plus the
        entry guard in :meth:`remove_module` already makes scaling a protected workload
        unreachable, while :meth:`restart_service` legitimately targets ``ollama``, which is
        protected from removal and allowlisted for restart at the same time.
        """
        labels = workload.get("metadata", {}).get("labels") or {}
        component = labels.get(COMPONENT_LABEL)
        if component != name:
            log.warning(
                "skipping workload whose component label does not match",
                requested=name,
                component=component,
                workload=_name_of(workload),
            )
            return False
        return True

    def _patch(self, path: str, body: dict[str, Any], *, what: str) -> None:
        self._request("PATCH", path, json=body, content_type=_MERGE_PATCH, what=what)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        content_type: str | None = None,
        what: str,
    ) -> dict[str, Any]:
        """One API call, with every failure mode collapsed to "deferred".

        RBAC refusals, an unreachable API server and a malformed reply all mean the same thing
        to the operator — the workload is still running and will be until the next restart — so
        they raise :class:`ContainerControlDeferred` and the caller reports the removal as
        deferred rather than failed. Only a *refusal by policy* (a protected name) is an error.
        """
        headers = {"Accept": "application/json"}
        token = _read(self._token_path) if self._token_path else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = self._client.request(method, path, params=params, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise ContainerControlDeferred(f"could not {what}: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text.strip()[:200]
            raise ContainerControlDeferred(
                f"could not {what}: kubernetes API returned {response.status_code} {detail}"
            )
        try:
            payload = response.json()
        except ValueError as exc:  # a 2xx that isn't JSON — treat as unreachable, not a crash
            raise ContainerControlDeferred(f"could not {what}: malformed API reply") from exc
        return payload if isinstance(payload, dict) else {}


def _name_of(workload: dict[str, Any]) -> str | None:
    name = workload.get("metadata", {}).get("name")
    return name if isinstance(name, str) and name else None


def _read(path: Path | None) -> str | None:
    """Read a ServiceAccount file, or ``None`` if it is missing/unreadable (never raises)."""
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
