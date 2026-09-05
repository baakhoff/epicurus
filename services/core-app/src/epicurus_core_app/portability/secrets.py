"""The module half of the archive's secret inventory (#875).

Provider keys and connected accounts are the core's own credentials, and the export already
names them. A **module's** credentials are the blind spot this closes: the core writes them
to OpenBao on the module's behalf — a chat bridge's bot token is the pure case — and a
module that persists no rows at all is therefore invisible to a fan-out that only walks
``portable`` modules. `messaging` exports nothing and still stops working on the far side
until Discord and Telegram are reconnected, and before this the archive said nothing about
it.

So the inventory reads the *manifests*, not the modules: every enabled module declares the
OpenBao paths it needs in ``secrets[]``, and each of those paths is **probed** for the
tenant. Probed, never read — the value is discarded by the shape of the call, so no secret
material can reach the manifest even by accident (ADR-0133: no secret material in an
archive, at any strength).
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any, Protocol

from epicurus_core import get_logger

__all__ = ["ModuleSecretSource", "SecretPresence", "collect_module_secrets"]

log = get_logger("core.portability.secrets")


class SecretPresence(Protocol):
    """The one thing this needs of a secret store: "is there something at this path?"

    Narrowed to a protocol rather than taking a :class:`~epicurus_core.SecretStore` so the
    inventory cannot grow a use for the *value* it is handed back.
    """

    async def get(self, path: str, tenant_id: str | None = None) -> dict[str, Any]: ...


class ModuleSecretSource(Protocol):
    """The registry, narrowed to the snapshot walk."""

    async def snapshot(self, *, force: bool = False) -> list[Any]: ...


async def collect_module_secrets(
    registry: ModuleSecretSource, secrets: SecretPresence, *, tenant: str
) -> dict[str, list[str]]:
    """Per enabled module, the OpenBao paths it declares that this tenant actually holds.

    Best-effort at both levels, like the rest of the inventory: a vault that cannot be
    reached (or a registry that cannot be walked) yields an empty map rather than failing an
    otherwise good export — the archive is worth having without it, and a missing line here
    costs the operator a reconnect they would have discovered anyway.
    """
    found: dict[str, list[str]] = {}
    try:
        snapshots = await registry.snapshot()
    except Exception as exc:  # a registry we could not walk is not a failed export
        log.warning("module secret inventory could not list modules", error=str(exc))
        return {}
    for snap in snapshots:
        if getattr(snap, "removed", False) or not getattr(snap, "enabled", True):
            continue
        manifest = snap.manifest
        present: list[str] = []
        for path in manifest.secrets:
            with suppress(Exception):  # absent, or a vault that would not answer
                await secrets.get(path, tenant)
                present.append(path)
        if present:
            found[str(manifest.name)] = sorted(present)
    return dict(sorted(found.items()))
