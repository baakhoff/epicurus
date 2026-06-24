"""Tightly-scoped Docker control for confirmed module removal (#127, ADR-0028).

Removing a module deletes its **container** — a privileged action the UI gates behind a
confirm dialog. The core reaches the Docker socket (mounted read-write into the core
container) **only** through this class, which refuses to touch anything but a *known
module's own* container:

* never ``core-app``, ``web``, or a data-plane / infra service (a hard denylist, on top of
  the registry only ever passing a *configured module* name here);
* only within the core's **own Compose project**, so a co-located stack — e.g. the CI
  smoke run sitting next to a developer's dev stack — is never disturbed.

The Docker socket is root-equivalent on the host, so this is the single audited code path
that uses it, and it is deliberately separate from the safe enable/disable flag (#126),
which never touches Docker. If the socket is absent or forbidden, the controller is simply
unavailable and removal returns ``503`` — it never breaks core startup.
"""

from __future__ import annotations

import os
import socket
from typing import Any

from epicurus_core import get_logger

log = get_logger("epicurus_core_app.docker_control")

# Never removable, even if mis-configured as a module: the core itself, the web shell, and
# every data-plane / infra service. The primary guard is that only a *configured module*
# name is ever passed here; this denylist is defence-in-depth.
PROTECTED: frozenset[str] = frozenset(
    {
        "core-app",
        "web",
        "postgres",
        "valkey",
        "nats",
        "qdrant",
        "openbao",
        "minio",
        "minio-init",
        "traefik",
        "ollama",
        "searxng",
        "grafana",
        "loki",
        "prometheus",
        "tempo",
        "alertmanager",
        "otel-collector",
        "alloy",
    }
)

# Allowlisted for a non-destructive **restart** (never removal): infra containers that read a
# setting only at startup, so applying an operator's choice means bouncing them. A restart keeps
# the container, its volumes, and config — the only effect is re-reading env (#307, ADR-0046).
RESTARTABLE: frozenset[str] = frozenset({"ollama"})

_SERVICE_LABEL = "com.docker.compose.service"
_PROJECT_LABEL = "com.docker.compose.project"


class DockerError(RuntimeError):
    """Raised when a module's container cannot be removed (protected, or a Docker failure)."""


class DockerController:
    """Stops + removes a *module's own* container, scoped to the core's Compose project."""

    def __init__(self, client: Any, *, project: str | None = None) -> None:
        self._client = client
        self._project = project

    @classmethod
    def from_env(cls) -> DockerController | None:
        """Connect to the Docker socket, or return ``None`` if it is unavailable.

        Best-effort: a missing or forbidden socket disables removal (the endpoint then
        returns ``503``) rather than breaking core startup.
        """
        try:
            import docker  # lazy import — the SDK is only needed when the socket is mounted
        except Exception as exc:  # pragma: no cover - import guard
            log.warning("docker SDK unavailable; module removal disabled", error=str(exc))
            return None
        try:
            client = docker.from_env()
            project = cls._detect_project(client)
            log.info("docker control ready", project=project)
            return cls(client, project=project)
        except Exception as exc:
            log.warning("docker socket unavailable; module removal disabled", error=str(exc))
            return None

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

        Raises :class:`DockerError` for a protected name. A name with no matching
        container is a no-op (returns 0) — removal is idempotent, which also lets the
        startup tombstone reconcile re-run safely.
        """
        if name in PROTECTED:
            raise DockerError(f"{name!r} is protected and cannot be removed")
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
        except DockerError:
            raise
        except Exception as exc:
            raise DockerError(f"failed to remove {name!r}: {exc}") from exc

    def restart_service(self, name: str) -> bool:
        """Restart an allowlisted infra container in this Compose project; ``True`` if one was.

        Non-destructive (the container, its volumes and config survive) and far narrower than
        :meth:`remove_module`: only a name in :data:`RESTARTABLE` is permitted, so this can bounce
        Ollama to apply a start-up setting (#307) but nothing else. A name with no matching
        container is a no-op (``False``).
        """
        if name not in RESTARTABLE:
            raise DockerError(f"{name!r} is not restartable")
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
            raise DockerError(f"failed to restart {name!r}: {exc}") from exc
