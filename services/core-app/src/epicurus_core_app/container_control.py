"""The container-runtime seam (#891): one audited control surface, three runtimes.

Two places in the core need to touch the *container* runtime it happens to be deployed on:
removing a module (stop + delete its container, #127) and restarting Ollama after a
KV-cache change (#307). Both used to mean "talk to a Docker daemon" — a fair assumption
under Compose and a wrong one on Kubernetes, where there is no daemon and the core would
silently defer forever.

This module holds what is *runtime-neutral* about that path — the policy (:data:`PROTECTED`,
:data:`RESTARTABLE`), the error taxonomy, the :class:`ContainerController` protocol both
implementations satisfy, and the startup selection — so each runtime implementation stays a
thin adapter:

* :mod:`epicurus_core_app.docker_control` — Docker, unchanged (proxy by default, ADR-0109);
* :mod:`epicurus_core_app.kubernetes_control` — in-cluster, via the pod's ServiceAccount;
* *none* — no controller at all, announced once at startup.

**Unavailable is never fatal, and never disables removal** (ADR-0056/#382): a missing or
refusing runtime leaves ``controller`` as ``None`` (or raises
:class:`ContainerControlDeferred` mid-call), the module is still tombstoned and hidden
immediately, and only *stopping its container* — and applying an Ollama KV-cache change —
waits for the next restart. The whole seam is deliberately separate from the safe
enable/disable flag (#126), which never touches a runtime at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from epicurus_core import get_logger

log = get_logger("epicurus_core_app.container_control")

# Never removable, even if mis-configured as a module: the core itself, the web shell, and
# every data-plane / infra service. The primary guard is that only a *configured module*
# name is ever passed here; this denylist is defence-in-depth. Runtime-neutral on purpose —
# the Docker and Kubernetes adapters enforce exactly the same list.
PROTECTED: frozenset[str] = frozenset(
    {
        "core-app",
        # The reserved in-process pseudo-module name (ADR-0093 §2) — the core answering its own
        # ``review`` page. It has no container at all, so this entry is purely belt-and-braces:
        # ``ModuleRegistry`` already refuses every management write addressed to this name.
        "core",
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

# Allowlisted for a non-destructive **restart** (never removal): infra workloads that read a
# setting only at startup, so applying an operator's choice means bouncing them. A restart keeps
# the workload, its volumes, and config — the only effect is re-reading env (#307, ADR-0046).
RESTARTABLE: frozenset[str] = frozenset({"ollama"})

#: The runtimes ``CONTAINER_RUNTIME`` accepts. ``auto`` resolves to one of the other three.
RUNTIMES: frozenset[str] = frozenset({"auto", "docker", "kubernetes", "none"})

#: Where Docker's socket lives when it is bind-mounted (the ADR-0099 overlay); its presence is
#: one of the two signals ``auto`` uses to pick the Docker adapter.
DOCKER_SOCKET = Path("/var/run/docker.sock")


class ContainerControlError(RuntimeError):
    """A container action was refused or failed (protected name, or a runtime failure)."""


class ContainerControlDeferred(ContainerControlError):
    """The action could not be performed *now* — permission denied, or the API unreachable.

    Distinct from its parent because the two mean different things to the operator: a plain
    :class:`ContainerControlError` is a refusal to be shown as an error (removing a protected
    name), while this one is the ordinary degraded mode — the module is still tombstoned and
    hidden, and only its container's teardown waits for the next restart. The caller reports
    it as *deferred*, never as a failed removal.
    """


class ContainerController(Protocol):
    """What the core asks of a container runtime — nothing more, ever.

    Both methods are **synchronous**: callers run them on a worker thread
    (``asyncio.to_thread``), which keeps a blocking Docker SDK call and a blocking HTTP call
    to the Kubernetes API the same shape at the call site.
    """

    def remove_module(self, name: str) -> int:
        """Take down a *known module's own* workload; return how many were taken down.

        Idempotent: a name with no matching workload is a no-op returning ``0`` (the startup
        tombstone reconcile re-runs this safely). Raises :class:`ContainerControlError` for a
        :data:`PROTECTED` name, :class:`ContainerControlDeferred` when the runtime cannot be
        reached or refuses.
        """

    def restart_service(self, name: str) -> bool:
        """Restart an allowlisted infra workload; ``True`` if one was.

        Non-destructive and far narrower than :meth:`remove_module`: only a name in
        :data:`RESTARTABLE` is permitted. A name with no matching workload is a no-op
        (``False``).
        """


@dataclass(frozen=True)
class ContainerAvailability:
    """The result of selecting (and probing) a container runtime at startup (#622, #891).

    ``controller`` is ``None`` exactly when ``reason`` explains why — never both set, never
    both empty. Kept as one value (not a bare ``ContainerController | None``) so the *reason*
    an operator sees on the Modules page is the real text, not a guess reconstructed later
    from nothing. ``runtime`` is the resolved runtime name (never ``auto``), for logs.
    """

    controller: ContainerController | None
    reason: str | None = None
    runtime: str = "none"


def resolve_runtime(configured: str) -> str:
    """Resolve ``CONTAINER_RUNTIME`` to a concrete runtime name.

    ``auto`` (the default) looks at the environment the core actually runs in, in the only
    order that can't be ambiguous: a pod always has ``KUBERNETES_SERVICE_HOST``; a Compose
    deployment always has ``DOCKER_HOST`` (the proxy, #708) or the mounted socket; anything
    else has no container control at all. An unknown value degrades to ``auto`` rather than
    failing startup.
    """
    choice = configured.strip().lower() or "auto"
    if choice not in RUNTIMES:
        log.warning("unknown CONTAINER_RUNTIME; falling back to auto", configured=configured)
        choice = "auto"
    if choice != "auto":
        return choice
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    if os.environ.get("DOCKER_HOST") or DOCKER_SOCKET.exists():
        return "docker"
    return "none"


def select_controller(configured: str = "auto", *, namespace: str = "") -> ContainerAvailability:
    """Pick and probe the container controller for this deployment; never raises.

    Logs exactly **one** line about the outcome, at startup — the ``none`` runtime says once
    that container teardown is unavailable rather than repeating it on every call (#891).
    """
    runtime = resolve_runtime(configured)
    if runtime == "kubernetes":
        from epicurus_core_app.kubernetes_control import KubernetesController

        return KubernetesController.from_env(namespace=namespace)
    if runtime == "docker":
        from epicurus_core_app.docker_control import DockerController

        return DockerController.from_env()
    reason = (
        "container control is disabled (CONTAINER_RUNTIME=none)"
        if configured.strip().lower() == "none"
        else "no container runtime detected (no Kubernetes service account, no DOCKER_HOST "
        "and no Docker socket)"
    )
    log.info("container control unavailable; container teardown deferred", reason=reason)
    return ContainerAvailability(controller=None, reason=reason, runtime="none")
