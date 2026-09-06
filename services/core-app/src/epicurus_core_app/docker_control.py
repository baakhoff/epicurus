"""Tightly-scoped Docker control for confirmed module removal (#127, ADR-0028, ADR-0099, ADR-0109).

The Docker arm of the container-runtime seam (#891): one of the implementations of
:class:`~epicurus_core_app.container_control.ContainerController`, selected under Compose and
unchanged in behavior by that seam — everything below is what it has always done.

Removing a module deletes its **container** — a privileged action the UI gates behind a
confirm dialog. The core reaches Docker **only** through this class, which refuses to touch
anything but a *known module's own* container:

* never ``core-app``, ``web``, or a data-plane / infra service (a hard denylist, on top of
  the registry only ever passing a *configured module* name here);
* only within the core's **own Compose project**, so a co-located stack — e.g. the CI
  smoke run sitting next to a developer's dev stack — is never disturbed.

By default (#708, ADR-0109) this talks to ``docker-proxy-core`` — a filtered allowlist proxy
in front of the real socket, reached over ``DOCKER_HOST=tcp://docker-proxy-core:2375``
(``docker.from_env()`` below picks this up with no code change). The proxy only ever answers
the exact calls this class makes — list/inspect a container, stop/restart/remove by id — so
exec/create/attach/images/volumes/networks/system are refused before they ever reach the
socket, on top of this class's own denylist and project-scoping. The opt-in
``services/core-app/compose.docker-socket.yaml`` overlay points this at the raw socket
instead (ADR-0099) — a documented escape hatch, not the default. Either way, this is the
single audited code path that touches Docker, deliberately separate from the safe
enable/disable flag (#126), which never does. Docker being unreachable at all (neither proxy
nor raw socket, e.g. a host with no Docker) does **not** disable removal (ADR-0056/#382
decoupled the two): the module is still tombstoned and hidden immediately; only *deleting its
container* — and applying an Ollama KV-cache change (#307) — defers to the next restart.
"""

from __future__ import annotations

import os
import socket
from typing import Any

from epicurus_core import get_logger
from epicurus_core_app.container_control import (
    PROTECTED,
    RESTARTABLE,
    ContainerAvailability,
    ContainerControlError,
)

log = get_logger("epicurus_core_app.docker_control")

# The denylist (:data:`PROTECTED`) and the restart allowlist (:data:`RESTARTABLE`) are policy,
# not Docker mechanics, so they live on the seam and every runtime enforces the same two sets.

_SERVICE_LABEL = "com.docker.compose.service"
_PROJECT_LABEL = "com.docker.compose.project"


class DockerController:
    """Stops + removes a *module's own* container, scoped to the core's Compose project."""

    def __init__(self, client: Any, *, project: str | None = None) -> None:
        self._client = client
        self._project = project

    @classmethod
    def from_env(cls) -> ContainerAvailability:
        """Connect to Docker — by default ``docker-proxy-core`` over ``DOCKER_HOST``, or the
        raw socket under the ``compose.docker-socket.yaml`` overlay (ADR-0099); never raises.

        Best-effort: an unreachable proxy/socket **defers container teardown on module
        removal to the next restart** (ADR-0056/#382 decoupled removal itself from the live
        socket — it always succeeds) and leaves an Ollama KV-cache change unapplied until a
        manual restart (#307). It never blocks core startup either way.
        """
        try:
            import docker  # lazy import — the SDK is only needed when Docker is reachable
        except Exception as exc:  # pragma: no cover - import guard
            reason = str(exc)
            log.warning("docker SDK unavailable; container teardown deferred", error=reason)
            return ContainerAvailability(controller=None, reason=reason, runtime="docker")
        try:
            client = docker.from_env()
            project = cls._detect_project(client)
            log.info("docker control ready", project=project)
            return ContainerAvailability(controller=cls(client, project=project), runtime="docker")
        except Exception as exc:
            reason = str(exc)
            log.warning("docker unreachable; container teardown deferred", error=reason)
            return ContainerAvailability(controller=None, reason=reason, runtime="docker")

    @staticmethod
    def _detect_project(client: Any) -> str | None:
        """The Compose project the core runs in — so removal is scoped to this stack only.

        Read from the core's *own* container label (its hostname is the container id by
        default); falls back to ``COMPOSE_PROJECT_NAME``. ``None`` means "don't scope by
        project" — acceptable when only one stack runs on the host.
        """
        try:
            own = client.containers.get(socket.gethostname())
            label = own.labels.get(_PROJECT_LABEL)
            if label:
                return str(label)
        except Exception:
            pass
        return os.environ.get("COMPOSE_PROJECT_NAME") or None

    def remove_module(self, name: str) -> int:
        """Stop and remove *name*'s container(s); return how many were removed.

        Raises :class:`ContainerControlError` for a protected name. A name with no matching
        container is a no-op (returns 0) — removal is idempotent, which also lets the
        startup tombstone reconcile re-run safely.
        """
        if name in PROTECTED:
            raise ContainerControlError(f"{name!r} is protected and cannot be removed")
        label_filters = [f"{_SERVICE_LABEL}={name}"]
        if self._project:
            label_filters.append(f"{_PROJECT_LABEL}={self._project}")
        try:
            containers = self._client.containers.list(all=True, filters={"label": label_filters})
            removed = 0
            for container in containers:
                # Belt-and-suspenders: never touch a protected service even if a label
                # filter somehow matched one.
                if container.labels.get(_SERVICE_LABEL) in PROTECTED:
                    continue
                container.stop(timeout=10)
                container.remove(force=True)
                removed += 1
            return removed
        except ContainerControlError:
            raise
        except Exception as exc:
            raise ContainerControlError(f"failed to remove {name!r}: {exc}") from exc

    def restart_service(self, name: str) -> bool:
        """Restart an allowlisted infra container in this Compose project; ``True`` if one was.

        Non-destructive (the container, its volumes and config survive) and far narrower than
        :meth:`remove_module`: only a name in :data:`RESTARTABLE` is permitted, so this can bounce
        Ollama to apply a start-up setting (#307) but nothing else. A name with no matching
        container is a no-op (``False``).
        """
        if name not in RESTARTABLE:
            raise ContainerControlError(f"{name!r} is not restartable")
        label_filters = [f"{_SERVICE_LABEL}={name}"]
        if self._project:
            label_filters.append(f"{_PROJECT_LABEL}={self._project}")
        try:
            containers = self._client.containers.list(all=True, filters={"label": label_filters})
            restarted = 0
            for container in containers:
                if container.labels.get(_SERVICE_LABEL) != name:
                    continue  # defence-in-depth: only the exact allowlisted service
                container.restart(timeout=10)
                restarted += 1
            return restarted > 0
        except Exception as exc:
            raise ContainerControlError(f"failed to restart {name!r}: {exc}") from exc
