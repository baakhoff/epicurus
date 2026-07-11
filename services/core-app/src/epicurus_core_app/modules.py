"""The module registry — the core's view of installed modules (ADR-0004 / ADR-0007).

Discovers each configured module's manifest over the internal network and serves it
to the web shell: identity, tools, declared UI, health. Module config values
round-trip through the core into OpenBao (``modules/<name>/config``, tenant-scoped),
and manifest-declared UI actions invoke the module's MCP tools through the core —
the shell never talks to a module directly.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from epicurus_core import (
    AccountsView,
    CollectionPrefs,
    ModuleManifest,
    SecretError,
    SecretStore,
    get_logger,
)
from epicurus_core_app.agent.mcp_host import McpHost, ModuleUnreachableError, ToolCallError
from epicurus_core_app.docker_control import PROTECTED, DockerController, DockerError
from epicurus_core_app.module_prefs import ModulePrefsStore

log = get_logger("epicurus_core_app.modules")


def _safe_segment(value: str, *, label: str) -> str:
    """A caller-supplied URL path segment, rejected if it could escape the module path.

    ``ref_id`` / entity ``kind`` / ``page_id`` are interpolated straight into the
    outbound module request path; a separator or ``..`` could redirect the request to
    another path on the (trusted, internal) module host. Defense-in-depth (#175).
    """
    if not value or "/" in value or "\\" in value or ".." in value:
        raise HTTPException(status_code=400, detail=f"invalid {label}: {value!r}")
    return value


class ModuleStatus(BaseModel):
    """Liveness as seen from the core right now."""

    healthy: bool
    version: str | None = None


class ModuleSnapshot(BaseModel):
    """One installed module: its manifest, current status, and operator enable flag."""

    manifest: ModuleManifest
    status: ModuleStatus
    # The operator's enable/disable choice (#126). A disabled module keeps running but is
    # hidden from the agent's tools, the left-nav, and the chat surfaces; it still appears
    # here so the shell's Modules screen can show it with a re-enable toggle.
    enabled: bool = True
    # Tombstoned after a confirmed container removal (#127). The registry hides it from
    # every surface — the module list drops it before serialization — so it is always
    # ``False`` on the snapshots the API returns; the flag is an internal gating signal.
    removed: bool = False
    # Tool names the operator has explicitly disabled for this module (#213). The agent
    # never receives a disabled tool; the shell renders each as a toggleable row.
    disabled_tools: list[str] = Field(default_factory=list)


class EnabledUpdate(BaseModel):
    """The body of an enable/disable toggle (#126)."""

    enabled: bool


class ToolEnabledUpdate(BaseModel):
    """The body of a per-tool enable/disable toggle (#213)."""

    enabled: bool


class ModelsUpdate(BaseModel):
    """The body of a per-module model-slot update (#128): ``{slot_key: model_id}``."""

    models: dict[str, str]


class SuggestionsEnabledUpdate(BaseModel):
    """The body of the suggestions-review on/off toggle (#KB-refactor)."""

    enabled: bool


class ToolInvocation(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    result: str


class DocSave(BaseModel):
    """The body of an editor-document save: the full new content (ADR-0018)."""

    content: str


class MoveRequest(BaseModel):
    """Request body for a file/folder move or rename (#216)."""

    from_path: str
    to_path: str


class ApproveRequest(BaseModel):
    """Optional approve body (#KB-refactor): the operator's per-hunk-merged content for an
    edit. Absent ⇒ apply the agent's full proposal."""

    content: str | None = None


class MailboxSend(BaseModel):
    """The body of a mailbox page's human-initiated compose/reply (ADR-0087).

    Forwarded verbatim to the module's ``POST /pages/{id}/send``: a reply carries
    ``reply_to_message_id`` (the module re-derives threading), a fresh compose carries
    ``to``/``subject``. Operator-only (shell -> core -> module) — never an MCP tool, so the
    agent can't send (ADR-0085 holds).
    """

    body: str
    to: str | None = None
    subject: str | None = None
    cc: str | None = None
    reply_to_message_id: str | None = None


class ModuleRegistry:
    """Fetches module manifests/health and routes UI actions to module tools."""

    # Network probes are cached per base for this long before a caller triggers a fresh
    # one (#478); an unhealthy entry expires sooner so a recovery shows up promptly.
    _HEALTHY_PROBE_TTL_S = 15.0
    _UNHEALTHY_PROBE_TTL_S = 5.0

    def __init__(
        self,
        base_urls: list[str],
        *,
        mcp: McpHost,
        secrets: SecretStore,
        tenant: str,
        prefs: ModulePrefsStore,
        docker: DockerController | None = None,
    ) -> None:
        self._bases = list(base_urls)
        self._mcp = mcp
        self._secrets = secrets
        self._tenant = tenant
        self._prefs = prefs
        self._docker = docker
        # Per-base probe cache (#478), 1:1 with ``self._bases``: each base refreshes
        # independently and single-flight, so one hung module can never delay a call
        # routed to a different, healthy one.
        self._probe_cache: list[ModuleSnapshot | None] = [None] * len(self._bases)
        self._probed_at: list[float] = [0.0] * len(self._bases)
        self._probe_locks: list[asyncio.Lock] = [asyncio.Lock() for _ in self._bases]
        self._unprobed: set[int] = set(range(len(self._bases)))
        self._last_healthy: dict[str, bool] = {}  # name -> last observed health (log transitions)

    async def snapshot(self, *, force: bool = False) -> list[ModuleSnapshot]:
        """Every configured module — reachable ones with their manifest, dead ones flagged.

        Each snapshot carries the operator's ``enabled`` flag (#126) and a ``removed``
        tombstone (#127); unset defaults to enabled and not-removed. The list stays **1:1
        with the configured bases** (``_resolve`` / ``enabled_mcp_urls`` zip it against them),
        so disabled and removed modules are flagged in place, not dropped — disabled ones
        are still shown so the shell can re-enable them, while removed ones are dropped by
        the list endpoint.

        The network probe behind each entry is TTL-cached (#478) — this only re-probes
        bases whose cache entry is stale, never the whole fleet on every call. The
        operator-prefs overlay (``enabled``/``removed``/``disabled_tools``) is always read
        fresh, so a toggle takes effect immediately regardless of probe caching.
        ``force=True`` (the Modules page's manual refresh) bypasses the TTL fleet-wide.
        """
        probed = await asyncio.gather(
            *(self._probe_cached(i, force=force) for i in range(len(self._bases)))
        )
        enabled = await self._prefs.enabled_map(self._tenant)
        removed = await self._prefs.removed_modules(self._tenant)
        out: list[ModuleSnapshot] = []
        for snap in probed:
            overlaid = snap.model_copy()
            overlaid.enabled = enabled.get(overlaid.manifest.name, True)
            overlaid.removed = overlaid.manifest.name in removed
            overlaid.disabled_tools = sorted(
                await self._prefs.get_disabled_tools(self._tenant, overlaid.manifest.name)
            )
            out.append(overlaid)
        return out

    def _cache_fresh(self, index: int) -> bool:
        snap = self._probe_cache[index]
        if snap is None:
            return False
        ttl = self._HEALTHY_PROBE_TTL_S if snap.status.healthy else self._UNHEALTHY_PROBE_TTL_S
        return (time.monotonic() - self._probed_at[index]) < ttl

    async def _probe_cached(self, index: int, *, force: bool = False) -> ModuleSnapshot:
        """The base at ``index``'s cached-or-fresh probe result, single-flight per base.

        A TTL hit never touches the network. A miss (or ``force``) probes only *this*
        base — concurrent callers for the same stale base share one in-flight probe
        (double-checked locking) rather than each firing their own.
        """
        if not force and self._cache_fresh(index):
            cached = self._probe_cache[index]
            assert cached is not None
            return cached
        async with self._probe_locks[index]:
            if not force and self._cache_fresh(index):  # refreshed while we waited
                cached = self._probe_cache[index]
                assert cached is not None
                return cached
            snap = await self._probe(self._bases[index])
            self._probe_cache[index] = snap
            self._probed_at[index] = time.monotonic()
            self._unprobed.discard(index)
            return snap

    async def _probe(self, base: str) -> ModuleSnapshot:
        """One module's manifest + live health.

        Logs on health **transitions** only (#478): a WARN the instant a previously
        healthy module goes unreachable, an INFO the instant it recovers, and DEBUG for
        a module that has never yet been reachable (the startup grace window) — never a
        WARN per probe, which used to spam the console for ordinary boot/reconcile
        timing and for a busy module's normal retry cadence.
        """
        try:
            async with httpx.AsyncClient(base_url=base, timeout=5) as client:
                manifest_resp = await client.get("/manifest")
                manifest_resp.raise_for_status()
                manifest = ModuleManifest.model_validate(manifest_resp.json())
                health_resp = await client.get("/health")
                healthy = health_resp.status_code == 200
                version = (health_resp.json() or {}).get("version") if healthy else None
            error = None if healthy else f"health check returned {health_resp.status_code}"
            self._log_health_transition(manifest.name, healthy, error)
            return ModuleSnapshot(
                manifest=manifest, status=ModuleStatus(healthy=healthy, version=version)
            )
        except Exception as exc:  # a dead module is a fact to display, not an error
            name = urlsplit(base).hostname or base
            self._log_health_transition(name, False, repr(exc))
            return ModuleSnapshot(
                manifest=ModuleManifest(name=name, version="unknown"),
                status=ModuleStatus(healthy=False),
            )

    def _log_health_transition(self, name: str, healthy: bool, error: str | None) -> None:
        prev = self._last_healthy.get(name)
        self._last_healthy[name] = healthy
        if healthy:
            if prev is False:
                log.info("module recovered", module=name)
            return
        if prev is True:
            log.warning("module probe failed", module=name, error=error)
        elif prev is None:
            log.debug("module probe failed", module=name, error=error)
        # prev is False: steady-state unreachable — already reported, don't repeat.

    def _index_for_name(self, name: str) -> int | None:
        for i, snap in enumerate(self._probe_cache):
            if snap is not None and snap.manifest.name == name:
                return i
        return None

    async def _resolve(self, name: str) -> tuple[str, ModuleManifest]:
        """The base URL + manifest of the module called ``name`` (404 if absent).

        Routes via the probe cache and re-checks at most *name*'s own base — never the
        rest of the fleet (#478). The very first call for a not-yet-seen name still
        probes whatever bases haven't been probed yet (bounded to those, not a
        guaranteed full fan-out); a warm registry resolves a bad name with zero network
        calls.
        """
        index = self._index_for_name(name)
        if index is None and self._unprobed:
            await asyncio.gather(*(self._probe_cached(i) for i in list(self._unprobed)))
            index = self._index_for_name(name)
        if index is None:
            raise HTTPException(status_code=404, detail=f"no reachable module named {name!r}")
        snap = await self._probe_cached(index)
        if snap.status.healthy:
            return self._bases[index], snap.manifest
        raise HTTPException(status_code=404, detail=f"no reachable module named {name!r}")

    async def base_url(self, name: str) -> str:
        """The reachable base URL of the module called ``name`` (404 if unavailable).

        The public, health-gated way for in-core consumers — the Files view's object bridge
        (ADR-0063) and the messaging bridge-admin's reload control path (ADR-0062) — to reach a
        module; a disabled or down module raises rather than returning a stale address. The
        browser never sees these URLs; the core stays the sole gateway.
        """
        base, _ = await self._resolve(name)
        return base

    async def enabled_mcp_urls(self) -> list[str]:
        """The ``/mcp`` URLs of healthy, **enabled** modules — the agent's tool surface.

        Backs ``McpHost.discover`` so a disabled module's tools are never offered to the
        model (#126). Order follows ``self._bases`` (``snapshot`` preserves it).
        """
        snaps = await self.snapshot()
        return [
            f"{base}/mcp"
            for snap, base in zip(snaps, self._bases, strict=True)
            if snap.status.healthy and snap.enabled and not snap.removed
        ]

    async def _post_reindex(self, base: str) -> None:
        """POST ``{base}/reindex`` to one module (overridable in tests, like ``_probe``)."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{base}/reindex")
            resp.raise_for_status()

    async def reembed(self) -> list[dict[str, str]]:
        """Re-embed every reindexable, enabled module (#332).

        Fans out ``POST {base}/reindex`` to each healthy, enabled, non-removed module whose
        manifest declares ``reindexable`` — the action behind the Models page's "Re-embed
        everything" after the embedding model changes (vectors are model-specific). Best-effort
        per module: one module's failure is logged and reported, never aborts the rest. Each
        module re-embeds its own tenant's corpus (single-tenant in v1, so it matches ours).
        """
        snaps = await self.snapshot()
        results: list[dict[str, str]] = []
        for snap, base in zip(snaps, self._bases, strict=True):
            if not (snap.status.healthy and snap.enabled and not snap.removed):
                continue
            if not snap.manifest.reindexable:
                continue
            name = snap.manifest.name
            try:
                await self._post_reindex(base)
                results.append({"module": name, "status": "started"})
            except httpx.HTTPError as exc:
                log.warning("re-embed fan-out failed", module=name, error=str(exc))
                results.append({"module": name, "status": "error"})
        return results

    async def set_enabled(self, name: str, enabled: bool) -> None:
        """Enable or disable a module, persisting the operator's choice (#126).

        The container is untouched — only the core-side flag changes. 404 for a name that
        is not a configured module (reachable or not).
        """
        live = {snap.manifest.name for snap in await self.snapshot() if not snap.removed}
        if name not in live:
            raise HTTPException(status_code=404, detail=f"no module named {name!r}")
        await self._prefs.set_enabled(self._tenant, name, enabled)

    async def remove(self, name: str) -> dict[str, Any]:
        """Tombstone a module now, tearing its container down out-of-band (#127, #382, ADR-0028).

        Removal is **decoupled from the live Docker socket** (#382). The tombstone — a
        ``removed`` flag on ``module_prefs`` — is the source of truth: setting it hides the
        module from every surface and drops it from tool routing *immediately*, with or
        without Docker. When the socket is present we also stop + remove the container here;
        when it is absent we defer that teardown to the next startup reconcile
        (:meth:`reconcile_tombstones`), which already re-removes any tombstoned module whose
        container is still up. Either way the module is gone for the operator at once.

        404 for an unknown module; 403 for a protected/core service (enforced here regardless
        of the socket, *and* again in the Docker layer); otherwise 200. Idempotent: a module
        whose container is already gone still tombstones. The result's
        ``container_teardown_deferred`` is true when no socket was available, so the UI can say
        the container keeps running until the next restart.
        """
        live = {snap.manifest.name for snap in await self.snapshot() if not snap.removed}
        if name not in live:
            raise HTTPException(status_code=404, detail=f"no module named {name!r}")
        # Enforce the protected denylist before tombstoning — never persist a removal for a
        # core/data-plane service, even if (somehow) one were configured as a module. Protected
        # names usually aren't in ``live``; this is defence-in-depth, independent of the socket.
        if name in PROTECTED:
            raise HTTPException(
                status_code=403, detail=f"{name!r} is protected and cannot be removed"
            )
        containers = 0
        deferred = self._docker is None
        if not deferred:
            assert self._docker is not None  # narrowed by ``deferred`` for mypy
            try:
                containers = await asyncio.to_thread(self._docker.remove_module, name)
            except DockerError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        # Always tombstone — this hides the module everywhere and stops routing now, and is
        # re-enforced on the next startup so a ``compose up`` cannot silently resurrect it.
        await self._prefs.set_removed(self._tenant, name, True)
        log.info(
            "module removed",
            module=name,
            containers=containers,
            container_teardown_deferred=deferred,
        )
        return {"removed": name, "containers": containers, "container_teardown_deferred": deferred}

    async def reconcile_tombstones(self) -> None:
        """Re-remove any tombstoned module whose container is still up (#127, #382).

        Run at startup. It serves two cases: a removal surviving a ``compose up`` / Watchtower
        pull (which would recreate the container), **and** the deferred teardown of a module
        that was tombstoned while the core had no socket (#382) — both leave a tombstoned module
        with a live container, which this clears once a socket is available. Best-effort — a
        missing socket or a transient Docker error is logged, never fatal.
        """
        if self._docker is None:
            return
        for name in await self._prefs.removed_modules(self._tenant):
            try:
                containers = await asyncio.to_thread(self._docker.remove_module, name)
                if containers:
                    log.info("re-removed resurrected module", module=name, containers=containers)
            except Exception as exc:
                log.warning("tombstone reconcile failed", module=name, error=repr(exc))

    async def get_models(self, name: str) -> dict[str, str]:
        """The operator's per-slot model choices for *name* (#128). 404 if unknown."""
        await self._resolve(name)
        return await self._prefs.get_models(self._tenant, name)

    async def set_models(self, name: str, models: dict[str, str]) -> None:
        """Persist per-slot model choices for *name*, validating the slot keys (#128).

        Each key must be a slot the module declares in ``required_models``; a blank value
        clears that slot (falls back to the core default). 404 unknown module, 400 unknown slot.
        """
        _, manifest = await self._resolve(name)
        valid = {slot.key for slot in manifest.required_models}
        chosen = {key: model for key, model in models.items() if model}
        unknown = set(chosen) - valid
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown model slot(s): {sorted(unknown)}")
        await self._prefs.set_models(self._tenant, name, chosen)

    async def model_for_slot(self, name: str, slot: str) -> str | None:
        """The chosen model for *name*'s *slot*, or ``None`` to use the core default (#128).

        Reads the stored choice directly (no manifest round-trip) so a module can resolve
        its own slot cheaply; an unset slot or unknown module yields ``None`` → core default.
        """
        models = await self._prefs.get_models(self._tenant, name)
        return models.get(slot)

    async def get_suggestions_enabled(self, name: str) -> bool:
        """Whether agent changes to *name* go through review (default True) (#KB-refactor).

        Read directly from Postgres (no manifest round-trip, like ``model_for_slot``) so a
        module can resolve its own setting cheaply via ``PlatformClient`` to decide whether to
        stage a suggestion or apply the change directly.
        """
        return await self._prefs.get_suggestions_enabled(self._tenant, name)

    async def set_suggestions_enabled(self, name: str, enabled: bool) -> None:
        """Persist whether *name*'s agent changes go through review. 404 if unknown (operator)."""
        await self._resolve(name)  # only known modules
        await self._prefs.set_suggestions_enabled(self._tenant, name, enabled)

    async def accounts_view(self, name: str) -> AccountsView:
        """The module's connected accounts + collections, merged with the operator's prefs.

        Proxies the module's ``GET /accounts`` (live discovery) and folds the stored
        ``enabled`` / ``active`` selection onto each collection so the shell can render the
        connected-accounts section (ADR-0030). 404 if the module is unreachable or does not
        declare ``collections``. ``local`` is the silent default and never appears here.
        """
        base, manifest = await self._resolve(name)
        if manifest.collections is None:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no collections")
        view = AccountsView.model_validate(
            await self._get_json(base, "/accounts", op=f"{name} accounts")
        )
        prefs = await self._prefs.get_collections(self._tenant, name)
        enabled = {(ref.account, ref.collection) for ref in prefs.enabled}
        active = (prefs.active.account, prefs.active.collection) if prefs.active else None
        for account in view.accounts:
            for col in account.collections:
                key = (col.account, col.collection)
                col.enabled = key in enabled
                col.active = key == active
        return view

    async def set_collections(self, name: str, prefs: CollectionPrefs) -> None:
        """Persist the operator's enabled collections + active view for ``name`` (ADR-0030).

        Store-through: the refs are **not** live-validated against the module's ``/accounts``
        (a save must not depend on the module being reachable — the module ignores refs that
        no longer resolve). The only invariant enforced is that ``active`` — when set — is one
        of the ``enabled`` collections; a null ``active`` means "use the local default".
        404 if the module is unknown/unreachable or declares no ``collections``; 400 if
        ``active`` is not enabled.
        """
        _, manifest = await self._resolve(name)
        if manifest.collections is None:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no collections")
        if prefs.active is not None:
            enabled = {(ref.account, ref.collection) for ref in prefs.enabled}
            if (prefs.active.account, prefs.active.collection) not in enabled:
                raise HTTPException(
                    status_code=400, detail="active must be one of the enabled collections"
                )
        await self._prefs.set_collections(self._tenant, name, prefs)

    async def collection_prefs(self, name: str) -> CollectionPrefs:
        """The stored ``{enabled, active}`` a module reads to route its own ops (ADR-0030).

        Read directly from Postgres (no manifest round-trip, like ``model_for_slot``) so a
        module can resolve its selection cheaply via ``PlatformClient.get_collections``; an
        unset module yields empty prefs → the module falls back to its local default.
        """
        return await self._prefs.get_collections(self._tenant, name)

    async def autoconnect_collections(self, provider: str) -> list[str]:
        """Seed empty collection selections for modules using *provider*, on connect (#209).

        For each configured module that declares ``collections`` listing *provider*, if the
        operator has made no selection yet, enable all of that provider's discovered
        collections and make the first writable one active — so connecting an account once
        makes the module use it without manual toggling (ADR-0030). An existing selection is
        never overridden. Best-effort: a module that is down or errors is skipped, never
        fatal. Returns the names of the modules that were seeded.
        """
        seeded: list[str] = []
        for snap in await self.snapshot():
            spec = snap.manifest.collections
            name = snap.manifest.name
            if (
                spec is None
                or provider not in spec.providers
                or snap.removed
                or not snap.status.healthy
            ):
                continue
            existing = await self._prefs.get_collections(self._tenant, name)
            if existing.enabled or existing.active is not None:
                continue  # the operator already chose — don't override
            try:
                view = await self.accounts_view(name)
            except Exception as exc:
                log.warning("autoconnect: accounts unavailable", module=name, error=repr(exc))
                continue
            account = next(
                (a for a in view.accounts if a.account == provider and a.connected), None
            )
            if account is None or not account.collections:
                continue
            writable = [c for c in account.collections if c.writable]
            active = (writable[0] if writable else account.collections[0]).ref()
            prefs = CollectionPrefs(enabled=[c.ref() for c in account.collections], active=active)
            await self._prefs.set_collections(self._tenant, name, prefs)
            seeded.append(name)
            log.info("autoconnected module to provider", module=name, provider=provider)
        return seeded

    async def disconnect_collections(self, provider: str) -> list[str]:
        """Drop *provider* from every module's stored selection on disconnect (#209).

        Symmetric with :meth:`autoconnect_collections`: once the account is gone its
        collections vanish from ``/accounts``, so remove their refs from the stored
        selection — a selection that empties falls back to the local default, and an
        ``active`` on the dropped provider clears to local. Returns the modules changed.
        """
        cleared: list[str] = []
        for snap in await self.snapshot():
            spec = snap.manifest.collections
            name = snap.manifest.name
            if spec is None or provider not in spec.providers:
                continue
            prefs = await self._prefs.get_collections(self._tenant, name)
            new_enabled = [ref for ref in prefs.enabled if ref.account != provider]
            keep_active = (
                prefs.active if (prefs.active and prefs.active.account != provider) else None
            )
            if len(new_enabled) != len(prefs.enabled) or keep_active != prefs.active:
                await self._prefs.set_collections(
                    self._tenant, name, CollectionPrefs(enabled=new_enabled, active=keep_active)
                )
                cleared.append(name)
        return cleared

    async def disabled_tools_set(self) -> set[str]:
        """All explicitly disabled tool names across every enabled module (#213).

        Backs ``McpHost.discover`` so disabled tools are never offered to the model even
        when their module is enabled. Only tools from enabled, non-removed modules are
        considered; tools from a disabled module are excluded anyway by the URL filter.
        """
        result: set[str] = set()
        for snap in await self.snapshot():
            if snap.enabled and not snap.removed:
                result |= set(snap.disabled_tools)
        return result

    async def get_tool_enabled(self, name: str, tool: str) -> bool:
        """Whether ``tool`` is enabled for module ``name`` (default ``True``) (#213).

        404 if the module is unknown or unreachable; 404 if the tool is not declared
        by the module's manifest.
        """
        _, manifest = await self._resolve(name)
        if tool not in {t.name for t in manifest.tools}:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no tool {tool!r}")
        disabled = await self._prefs.get_disabled_tools(self._tenant, name)
        return tool not in disabled

    async def set_tool_enabled(self, name: str, tool: str, enabled: bool) -> None:
        """Enable or disable a single tool for module ``name`` (#213).

        The tool is removed from (or added to) the disabled set; the module keeps running
        and all other tools are unaffected. 404 if the module is unknown or unreachable,
        or if the tool is not declared by the module's manifest.
        """
        _, manifest = await self._resolve(name)
        if tool not in {t.name for t in manifest.tools}:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no tool {tool!r}")
        await self._prefs.set_tool_enabled(self._tenant, name, tool, enabled)

    async def invoke(self, name: str, tool: str, arguments: dict[str, Any]) -> str:
        """Run a module tool (a manifest-declared UI action) through the MCP host.

        A tool that runs but reports failure surfaces as a **400** carrying the tool's
        own message — previously the error text returned as a 200 "result" and the
        shell closed the form as if the action had worked (#435). A module that is
        unreachable or does not answer in time is a controlled **502** (cf.
        :meth:`_get_json`), never a raw ``NetworkError`` bubbling up through nginx — the
        failure every board/calendar action shared through this dispatch (#472).
        """
        base, manifest = await self._resolve(name)
        if not await self._prefs.is_enabled(self._tenant, name):
            raise HTTPException(status_code=403, detail=f"module {name!r} is disabled")
        if tool not in {t.name for t in manifest.tools}:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no tool {tool!r}")
        try:
            return await self._mcp.call(tool, arguments, f"{base}/mcp", tenant=self._tenant)
        except ToolCallError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ModuleUnreachableError as exc:
            raise HTTPException(
                status_code=502, detail=f"{name} action failed: module unreachable"
            ) from exc

    async def get_config(self, name: str) -> dict[str, Any]:
        """The module's stored config values (empty if never saved)."""
        try:
            return await self._secrets.get(f"modules/{name}/config", self._tenant)
        except SecretError:
            return {}

    async def set_config(self, name: str, values: dict[str, Any]) -> None:
        """Persist the module's config values (tenant-scoped, encrypted at rest)."""
        await self._resolve(name)  # only known modules
        await self._secrets.set(f"modules/{name}/config", values, self._tenant)

    async def _get_json(
        self,
        base: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        timeout: float = 10,
        op: str,
    ) -> Any:
        """GET JSON from a module, mapping any failure to a clean HTTP error (#209).

        A module being slow, down, or returning an error must never surface as an
        unhandled exception — which a gateway (nginx) turns into an opaque **502 Bad
        Gateway**. Upstream client errors (4xx) pass through as-is (e.g. a module's 404
        for a missing entity); a 5xx, a timeout, or a connection failure becomes a
        controlled ``502`` carrying *op*, so the shell shows a reason rather than a bare
        Bad Gateway. ``op`` is a short human label (e.g. ``"calendar status"``).
        """
        try:
            async with httpx.AsyncClient(base_url=base, timeout=timeout) as client:
                if params:
                    resp = await client.get(path, params=dict(params))
                else:
                    resp = await client.get(path)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise HTTPException(
                status_code=code if 400 <= code < 500 else 502,
                detail=f"{op} failed: module returned {code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{op} failed: module unreachable") from exc

    async def _post_json(
        self,
        base: str,
        path: str,
        *,
        json: Any,
        timeout: float = 30,
        op: str,
    ) -> Any:
        """POST JSON to a module, mapping any failure to a clean HTTP error (cf. :meth:`_get_json`).

        Used by the draft-first send path (ADR-0085, #563). Unlike ``_get_json`` it **preserves a
        4xx module's own ``detail``** (e.g. mail's reconnect / rate-limit hint on a Gmail 403), so
        the resuming turn can relay that hint to the model rather than a generic message; a 5xx,
        timeout, or connection failure still becomes a controlled ``502`` carrying *op*.
        """
        try:
            async with httpx.AsyncClient(base_url=base, timeout=timeout) as client:
                resp = await client.post(path, json=json)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if 400 <= code < 500:
                try:
                    detail = str(exc.response.json().get("detail") or f"{op} failed")
                except (ValueError, AttributeError):
                    detail = f"{op} failed: module returned {code}"
                raise HTTPException(status_code=code, detail=detail) from exc
            raise HTTPException(
                status_code=502, detail=f"{op} failed: module returned {code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{op} failed: module unreachable") from exc

    async def get_status(self, name: str) -> dict[str, Any]:
        """Proxy the module's declared ``status_url`` endpoint to the caller.

        The module's manifest must declare ``ui.status_url`` (e.g. ``/status``);
        the core fetches that path on the module and returns the JSON body.
        Returns 404 if the module is unreachable or has no ``status_url``; a slow or
        erroring status endpoint is a controlled ``502`` rather than a raw Bad Gateway (#209).
        """
        base, manifest = await self._resolve(name)
        status_url = manifest.ui.status_url if manifest.ui else None
        if not status_url:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no status_url")
        data: dict[str, Any] = await self._get_json(
            base, status_url, timeout=5, op=f"{name} status"
        )
        return data

    async def get_docs(self, name: str) -> dict[str, Any]:
        """Proxy the module's declared ``docs_url`` endpoint to the caller (#215).

        The module's manifest must declare ``docs_url`` (e.g. ``/docs``); the core
        fetches that path and returns the JSON body. The response shape is
        ``{"documents": [{"path": str, "content": str}]}``.
        Returns 404 if the module is unreachable or has no ``docs_url``.
        """
        base, manifest = await self._resolve(name)
        if not manifest.docs_url:
            raise HTTPException(status_code=404, detail=f"module {name!r} declares no docs_url")
        data: dict[str, Any] = await self._get_json(base, manifest.docs_url, op=f"{name} docs")
        return data

    async def get_page(
        self, name: str, page_id: str, *, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        """Proxy a module's page-data endpoint to the shell (ADR-0018).

        The page must be declared in the module's ``manifest.pages``; the core then
        fetches ``GET /pages/{page_id}`` on the module and returns its JSON body — the
        archetype's data shape, which the shell renders. Query ``params`` are forwarded
        verbatim, so a parameterized archetype (e.g. a ``calendar`` reading its
        ``start``/``end`` window) reads from the same proxied path. A module never serves
        UI markup. Returns 404 if the module is unreachable or declares no such page.
        Query params (e.g. ``path``, ``q``) are forwarded to the module as-is.
        """
        _safe_segment(page_id, label="page_id")
        base, manifest = await self._resolve(name)
        if page_id not in {p.id for p in manifest.pages}:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no page {page_id!r}")
        data: dict[str, Any] = await self._get_json(
            base, f"/pages/{page_id}", params=params, op=f"{name} page {page_id!r}"
        )
        return data

    async def _resolve_editor_page(self, name: str, page_id: str) -> str:
        """The base URL of *name*, asserting it declares an ``editor`` page ``page_id``.

        Only the ``editor`` archetype owns per-document read/write — a ``browser`` (or
        any other) page has no docs, so the doc paths 404 for it. Raises 404 if the
        module is unreachable, has no such page, or the page isn't an editor.
        """
        base, manifest = await self._resolve(name)
        page = next((p for p in manifest.pages if p.id == page_id), None)
        if page is None:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no page {page_id!r}")
        if page.archetype != "editor":
            raise HTTPException(status_code=404, detail=f"page {page_id!r} is not an editor")
        return base

    async def _resolve_movable_page(self, name: str, page_id: str) -> str:
        """The base URL of *name*, asserting page ``page_id`` supports move/rename (#391).

        Move is the one mutation a ``browser`` page shares with an ``editor`` (the Files
        browser renames/relocates its writable entries), so it is the only ``/pages`` proxy
        that accepts both archetypes; everything else stays editor-only. Raises 404 if the
        module is unreachable, has no such page, or the page is neither editor nor browser.
        """
        base, manifest = await self._resolve(name)
        page = next((p for p in manifest.pages if p.id == page_id), None)
        if page is None:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no page {page_id!r}")
        if page.archetype not in ("editor", "browser"):
            raise HTTPException(status_code=404, detail=f"page {page_id!r} does not support move")
        return base

    async def get_page_doc(self, name: str, page_id: str, path: str) -> dict[str, Any]:
        """Proxy a single editor document's content to the shell (ADR-0018).

        The shell fetches ``GET /pages/{page_id}/doc?path=<path>`` on the module and
        returns its ``{path, title, content}`` body. ``path`` is module-relative; the
        module is responsible for confining it (no traversal out of its store).
        """
        base = await self._resolve_editor_page(name, page_id)
        data: dict[str, Any] = await self._get_json(
            base, f"/pages/{page_id}/doc", params={"path": path}, op=f"{name} doc"
        )
        return data

    async def get_page_doc_versions(self, name: str, page_id: str, path: str) -> dict[str, Any]:
        """Proxy an editor document's save history to the shell (ADR-0046).

        ``GET /pages/{page_id}/doc/versions?path=<path>`` → ``{versions:[…]}`` newest-first.
        Version history is an ``editor`` capability, so a non-editor page 404s here.
        """
        base = await self._resolve_editor_page(name, page_id)
        data: dict[str, Any] = await self._get_json(
            base,
            f"/pages/{page_id}/doc/versions",
            params={"path": path},
            op=f"{name} doc versions",
        )
        return data

    async def get_page_doc_version(
        self, name: str, page_id: str, path: str, version: str
    ) -> dict[str, Any]:
        """Proxy one past version of an editor document to the shell (ADR-0046).

        ``GET /pages/{page_id}/doc/version?path=<path>&version=<id>`` → its
        ``{path, version_id, created_at, title, content}`` body; 404 if no such version.
        """
        base = await self._resolve_editor_page(name, page_id)
        data: dict[str, Any] = await self._get_json(
            base,
            f"/pages/{page_id}/doc/version",
            params={"path": path, "version": version},
            op=f"{name} doc version",
        )
        return data

    async def save_page_doc(
        self, name: str, page_id: str, path: str, content: str
    ) -> dict[str, Any]:
        """Proxy an editor document save to the module (ADR-0018).

        ``PUT /pages/{page_id}/doc?path=<path>`` with ``{content}``; the module writes
        the document and (for knowledge) re-indexes it. The write timeout is generous
        because saving may trigger an embed round-trip back through the core.
        """
        base = await self._resolve_editor_page(name, page_id)
        try:
            async with httpx.AsyncClient(base_url=base, timeout=60) as client:
                resp = await client.put(
                    f"/pages/{page_id}/doc", params={"path": path}, json={"content": content}
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                return data
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise HTTPException(
                status_code=code if 400 <= code < 500 else 502,
                detail=f"{name} doc save failed: module returned {code}",
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"{name} doc save failed: module unreachable"
            ) from exc

    async def create_page_folder(self, name: str, page_id: str, path: str) -> dict[str, Any]:
        """Proxy ``POST /pages/{page_id}/folder`` to the module (#216).

        Creates a directory at *path* within the module's editor store. 409 if
        it already exists; the module enforces path-safety.
        """
        base = await self._resolve_editor_page(name, page_id)
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.post(f"/pages/{page_id}/folder", params={"path": path})
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    async def create_page_project(
        self, name: str, page_id: str, project_name: str
    ) -> dict[str, Any]:
        """Proxy ``POST /pages/{page_id}/project`` to the module (#KB-refactor).

        Creates a new knowledge base (a top-level scope). 409 if it already exists,
        400 for an invalid name; the module enforces name-safety.
        """
        base = await self._resolve_editor_page(name, page_id)
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.post(f"/pages/{page_id}/project", params={"name": project_name})
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    async def delete_page_project(self, name: str, page_id: str, project_name: str) -> None:
        """Proxy ``DELETE /pages/{page_id}/project`` to the module (#340).

        Deletes a knowledge base (a top-level scope); the module also de-indexes its
        documents. 404 if it does not exist; the module enforces name-safety and the
        read-only (watch-mode) guard. A longer timeout than the other tree ops since
        de-indexing a large base touches one Qdrant delete per document.
        """
        base = await self._resolve_editor_page(name, page_id)
        async with httpx.AsyncClient(base_url=base, timeout=30) as client:
            resp = await client.delete(f"/pages/{page_id}/project", params={"name": project_name})
            resp.raise_for_status()

    async def delete_page_doc(self, name: str, page_id: str, path: str) -> None:
        """Proxy ``DELETE /pages/{page_id}/doc`` to the module (#216).

        Deletes a ``.md`` file at *path*. 404 if absent.
        """
        base = await self._resolve_editor_page(name, page_id)
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.delete(f"/pages/{page_id}/doc", params={"path": path})
            resp.raise_for_status()

    async def delete_page_folder(self, name: str, page_id: str, path: str) -> None:
        """Proxy ``DELETE /pages/{page_id}/folder`` to the module (#216).

        Deletes an empty directory at *path*. 409 if not empty.
        """
        base = await self._resolve_editor_page(name, page_id)
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.delete(f"/pages/{page_id}/folder", params={"path": path})
            resp.raise_for_status()

    async def move_page_item(
        self, name: str, page_id: str, from_path: str, to_path: str
    ) -> dict[str, Any]:
        """Proxy ``POST /pages/{page_id}/move`` to the module (#216).

        Moves or renames a file or folder from *from_path* to *to_path*. 409
        if the destination already exists, 404 if the source does not exist. Accepts
        ``editor`` (knowledge/notes) and ``browser`` (the storage Files page) archetypes.
        """
        base = await self._resolve_movable_page(name, page_id)
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.post(
                f"/pages/{page_id}/move",
                json={"from_path": from_path, "to_path": to_path},
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    async def _resolve_review_page(self, name: str, page_id: str) -> str:
        """The base URL of *name*, asserting it declares a ``review`` page ``page_id`` (#220).

        Only the ``review`` archetype owns the suggestion approve/reject surface — any
        other page type 404s for it. Raises 404 if the module is unreachable, has no such
        page, or the page isn't a review queue.
        """
        base, manifest = await self._resolve(name)
        page = next((p for p in manifest.pages if p.id == page_id), None)
        if page is None:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no page {page_id!r}")
        if page.archetype != "review":
            raise HTTPException(status_code=404, detail=f"page {page_id!r} is not a review page")
        return base

    async def review_action(
        self,
        name: str,
        page_id: str,
        suggestion_id: str,
        action: str,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Proxy an operator's approve/reject of a staged suggestion to the module (#220).

        ``POST /pages/{page_id}/suggestions/{suggestion_id}/{action}`` where *action* is
        ``approve`` (apply + index) or ``reject`` (discard) — both operator-only; the
        module never exposes them as agent tools. The timeout is generous because approve
        triggers a write + embed round-trip back through the core. *action* is supplied by
        the core's own route handlers (never the caller), so it needs no segment guard.

        On *approve*, *content* (optional) is the operator's per-hunk-merged result for an
        edit (#KB-refactor) — forwarded so only the approved part is written.
        """
        _safe_segment(page_id, label="page_id")
        _safe_segment(suggestion_id, label="suggestion_id")
        base = await self._resolve_review_page(name, page_id)
        kwargs: dict[str, Any] = {}
        if content is not None:
            kwargs["json"] = {"content": content}
        async with httpx.AsyncClient(base_url=base, timeout=60) as client:
            resp = await client.post(
                f"/pages/{page_id}/suggestions/{suggestion_id}/{action}", **kwargs
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    async def read_text(self, name: str, path: str) -> dict[str, Any]:
        """Proxy a module's inline text-read endpoint (#KB-refactor): ``GET /read?path=``.

        Returns ``{path, name, content}`` for a UTF-8 text file — used by the Files
        split-screen reader. Upstream 4xx pass through (415 binary, 413 too large, 404
        missing); an unreachable module is a controlled 502.
        """
        base, _ = await self._resolve(name)
        data: dict[str, Any] = await self._get_json(
            base, "/read", params={"path": path}, op=f"{name} read"
        )
        return data

    async def download(self, name: str, path: str) -> httpx.Response:
        """Proxy a binary file download from a module's ``/download`` endpoint.

        The module must be reachable; ``path`` is forwarded as-is. The caller is
        responsible for streaming the response body. 404 if the module is unreachable.
        """
        base, _ = await self._resolve(name)
        client = httpx.AsyncClient(base_url=base, timeout=60)
        try:
            resp = await client.get("/download", params={"path": path})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            await client.aclose()
            code = exc.response.status_code
            raise HTTPException(
                status_code=code if 400 <= code < 500 else 502,
                detail=f"{name} download failed: module returned {code}",
            ) from exc
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(
                status_code=502, detail=f"{name} download failed: module unreachable"
            ) from exc
        return resp

    async def resolve_entity(self, name: str, kind: str, ref_id: str) -> dict[str, Any]:
        """Proxy a module's hover-card resolver to the shell (ADR-0019).

        The module's manifest must set ``resolver`` true; the core then fetches
        ``GET /resolve/{kind}/{ref_id}`` on the module and returns the hover-card
        envelope. 404 if the module is unreachable or declares no resolver.
        """
        _safe_segment(kind, label="kind")
        _safe_segment(ref_id, label="ref_id")
        base, manifest = await self._resolve(name)
        if not manifest.resolver:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no resolver")
        data: dict[str, Any] = await self._get_json(
            base, f"/resolve/{kind}/{ref_id}", op=f"{name} resolve {kind}"
        )
        return data

    async def list_attachments(self, name: str) -> list[dict[str, Any]]:
        """Proxy a module's attachment picker (ADR-0019): ``GET /attachments``.

        The manifest must set ``attachable`` true; returns the module's attachable items
        (each ``{ref_id, kind, title}``). 404 if unreachable or not attachable.
        """
        base, manifest = await self._resolve(name)
        if not manifest.attachable:
            raise HTTPException(status_code=404, detail=f"module {name!r} is not attachable")
        items: list[dict[str, Any]] = await self._get_json(
            base, "/attachments", op=f"{name} attachments"
        )
        return items

    async def resolve_attachment(self, name: str, ref_id: str) -> dict[str, Any]:
        """Proxy a module's attachment resolve (ADR-0019): ``GET /attachments/{ref_id}``.

        Returns the entity's content/excerpt for the agent to inject into the turn. 404 if
        the module is unreachable or not attachable.
        """
        _safe_segment(ref_id, label="ref_id")
        base, manifest = await self._resolve(name)
        if not manifest.attachable:
            raise HTTPException(status_code=404, detail=f"module {name!r} is not attachable")
        data: dict[str, Any] = await self._get_json(
            base, f"/attachments/{ref_id}", op=f"{name} attachment resolve"
        )
        return data

    async def read_message(self, name: str, ref_id: str) -> dict[str, Any]:
        """Proxy a module's full-message endpoint to the panel (ADR-0019).

        Fetches ``GET /messages/{ref_id}`` on the module and returns the
        EmailMessage envelope — subject, from, date, and body — consumed by the
        right-panel ``email-reader`` view. 404 if the module is unreachable.
        """
        _safe_segment(ref_id, label="ref_id")
        base, _ = await self._resolve(name)
        data: dict[str, Any] = await self._get_json(
            base, f"/messages/{ref_id}", op=f"{name} message"
        )
        return data

    async def send_draft(self, name: str, draft: dict[str, Any]) -> str:
        """Transmit an operator-confirmed draft via the module's ``POST /send`` (ADR-0085, #563).

        The single point at which the core sends an outbound draft on the operator's behalf: after
        a Confirm it POSTs the exact composed ``draft`` to the module (mail's transmit endpoint),
        which sends it verbatim and returns the provider message id. It is a plain module endpoint,
        never an MCP tool, so the agent cannot reach it — the draft-first guarantee. Raises
        ``HTTPException`` carrying the module's own hint (e.g. a Gmail reconnect prompt) on failure.
        """
        base, _ = await self._resolve(name)
        data: dict[str, Any] = await self._post_json(base, "/send", json=draft, op=f"{name} send")
        return str(data.get("id", ""))

    async def _resolve_mailbox_page(self, name: str, page_id: str) -> str:
        """The base URL of *name*, asserting it declares a ``mailbox`` page ``page_id`` (ADR-0087).

        The mailbox send + attachment proxies are ``mailbox``-only — a non-mailbox page has
        neither, so they 404 for it (mirrors the editor-doc gate). Raises 404 if the module
        is unreachable, has no such page, or the page isn't a mailbox.
        """
        base, manifest = await self._resolve(name)
        page = next((p for p in manifest.pages if p.id == page_id), None)
        if page is None:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no page {page_id!r}")
        if page.archetype != "mailbox":
            raise HTTPException(status_code=404, detail=f"page {page_id!r} is not a mailbox")
        return base

    async def send_page_message(
        self, name: str, page_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Proxy a mailbox page's human-initiated compose/reply to the module (ADR-0087).

        The operator-only counterpart to :meth:`send_draft`: it POSTs the page's send request
        to the module's ``POST /pages/{page_id}/send`` (which composes/derives + transmits).
        Gated on the ``mailbox`` archetype, and reachable only from the shell — never an MCP
        tool, so the agent still cannot send (ADR-0085 holds). A module 4xx (e.g. a Gmail
        reconnect / rate-limit hint) is relayed with its own ``detail`` via ``_post_json``.
        """
        _safe_segment(page_id, label="page_id")
        base = await self._resolve_mailbox_page(name, page_id)
        data: dict[str, Any] = await self._post_json(
            base, f"/pages/{page_id}/send", json=payload, op=f"{name} mailbox send"
        )
        return data

    async def download_page_attachment(
        self, name: str, page_id: str, message_id: str, attachment_id: str
    ) -> httpx.Response:
        """Proxy a mailbox attachment download from the module (ADR-0087).

        Streams the bytes provider -> module -> here -> browser; nothing is stored. Gated on
        the ``mailbox`` archetype. The caller streams the returned response body. A module 404
        (unknown message/attachment) is relayed as 404; other 4xx as themselves, 5xx as 502.
        """
        _safe_segment(page_id, label="page_id")
        base = await self._resolve_mailbox_page(name, page_id)
        client = httpx.AsyncClient(base_url=base, timeout=60)
        try:
            resp = await client.get(
                f"/pages/{page_id}/attachment",
                params={"message_id": message_id, "attachment_id": attachment_id},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            await client.aclose()
            code = exc.response.status_code
            raise HTTPException(
                status_code=code if 400 <= code < 500 else 502,
                detail=f"{name} attachment failed: module returned {code}",
            ) from exc
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(
                status_code=502, detail=f"{name} attachment failed: module unreachable"
            ) from exc
        return resp

    async def all_suggestions(self) -> list[dict[str, Any]]:
        """Pending suggestions across every enabled module that declares a ``review`` page.

        Each item carries ``module`` + ``page_id`` so the shell can approve/reject it — the
        chat composer's suggestion bubble and the Suggestions page both read this feed
        (#KB-refactor). Best-effort: a module that is down, disabled, removed, or erroring
        is skipped rather than failing the whole feed.
        """
        out: list[dict[str, Any]] = []
        for snap, base in zip(await self.snapshot(), self._bases, strict=True):
            if snap.removed or not snap.enabled or not snap.status.healthy:
                continue
            for page in snap.manifest.pages:
                if page.archetype != "review":
                    continue
                try:
                    data = await self._get_json(
                        base, f"/pages/{page.id}", op=f"{snap.manifest.name} suggestions"
                    )
                except HTTPException:
                    continue
                for item in data.get("suggestions", []):
                    out.append({**item, "module": snap.manifest.name, "page_id": page.id})
        return out


def create_suggestions_router(registry: ModuleRegistry) -> APIRouter:
    """The cross-module pending-suggestions feed the shell polls (#KB-refactor).

    Separate from the modules router so it lives at ``/platform/v1/suggestions`` rather
    than under ``/platform/v1/modules``.
    """
    router = APIRouter(prefix="/platform/v1", tags=["suggestions"])

    @router.get("/suggestions")
    async def list_suggestions() -> list[dict[str, Any]]:
        return await registry.all_suggestions()

    return router


def create_modules_router(registry: ModuleRegistry) -> APIRouter:
    """The module surface the web shell renders (list, config, actions)."""
    router = APIRouter(prefix="/platform/v1/modules", tags=["modules"])

    @router.get("", response_model=list[ModuleSnapshot])
    async def list_modules(refresh: bool = Query(False)) -> list[ModuleSnapshot]:
        # Drop tombstoned modules — a removed module is gone, not merely disabled (#127).
        # ``?refresh=true`` bypasses the probe cache for a fleet-wide re-probe — the
        # Modules page's manual refresh (#478); the default read serves from cache.
        return [snap for snap in await registry.snapshot(force=refresh) if not snap.removed]

    @router.post("/reembed")
    async def reembed() -> dict[str, Any]:
        """Re-embed every reindexable module (#332) — the Models page's "Re-embed everything"
        after the embedding model changes. Fans out to each module's ``/reindex`` and returns a
        per-module status. A literal route, so it's declared before the ``/{name}/…`` paths."""
        return {"modules": await registry.reembed()}

    @router.get("/{name}/config")
    async def get_config(name: str) -> dict[str, Any]:
        return await registry.get_config(name)

    @router.put("/{name}/config")
    async def set_config(name: str, values: dict[str, Any]) -> dict[str, str]:
        await registry.set_config(name, values)
        return {"status": "ok"}

    @router.post("/{name}/enabled")
    async def set_module_enabled(name: str, body: EnabledUpdate) -> dict[str, str]:
        """Enable or disable a module (#126) — hides its tools/pages/UI; container stays up."""
        await registry.set_enabled(name, body.enabled)
        return {"status": "ok"}

    @router.delete("/{name}")
    async def remove_module(name: str) -> dict[str, Any]:
        """Confirmed module removal (#127, #382): tombstone the module, tear its container down.

        Privileged — gated by a confirm dialog in the shell. **Decoupled from the live Docker
        socket** (#382): the module is tombstoned (hidden everywhere, dropped from routing)
        immediately, so this soft-removes with **200** even when the core has no Docker access —
        the container teardown is then **deferred** to the next startup reconcile. The response
        carries ``container_teardown_deferred`` (true when no socket was available, so the
        container is still running until the next restart). Still **403** for a protected
        service and **404** for an unknown module.
        """
        return await registry.remove(name)

    @router.get("/{name}/models")
    async def get_module_models(name: str) -> dict[str, dict[str, str]]:
        """The module's per-slot model selections (#128)."""
        return {"models": await registry.get_models(name)}

    @router.put("/{name}/models")
    async def set_module_models(name: str, body: ModelsUpdate) -> dict[str, str]:
        """Set the module's per-slot model selections (#128); validates slot keys."""
        await registry.set_models(name, body.models)
        return {"status": "ok"}

    @router.get("/{name}/models/{slot}")
    async def get_module_model_slot(name: str, slot: str) -> dict[str, str | None]:
        """Resolve one slot to its chosen model, or ``null`` for the core default (#128)."""
        return {"model": await registry.model_for_slot(name, slot)}

    @router.get("/{name}/suggestions-enabled")
    async def get_module_suggestions_enabled(name: str) -> dict[str, bool]:
        """Whether agent changes to the module go through review (default on, #KB-refactor)."""
        return {"enabled": await registry.get_suggestions_enabled(name)}

    @router.put("/{name}/suggestions-enabled")
    async def set_module_suggestions_enabled(
        name: str, body: SuggestionsEnabledUpdate
    ) -> dict[str, str]:
        """Turn review on/off for the module (off ⇒ the agent's changes auto-apply)."""
        await registry.set_suggestions_enabled(name, body.enabled)
        return {"status": "ok"}

    @router.get("/{name}/collections", response_model=AccountsView)
    async def get_module_collections(name: str) -> AccountsView:
        """Connected accounts + collections, merged with the operator's selection (ADR-0030)."""
        return await registry.accounts_view(name)

    @router.put("/{name}/collections")
    async def set_module_collections(name: str, prefs: CollectionPrefs) -> dict[str, str]:
        """Persist the operator's enabled collections + active view (ADR-0030)."""
        await registry.set_collections(name, prefs)
        return {"status": "ok"}

    @router.get("/{name}/collections/prefs", response_model=CollectionPrefs)
    async def get_module_collection_prefs(name: str) -> CollectionPrefs:
        """The stored {enabled, active} a module reads to route its own ops (ADR-0030)."""
        return await registry.collection_prefs(name)

    @router.post("/{name}/tools/{tool}/enabled")
    async def set_tool_enabled(name: str, tool: str, body: ToolEnabledUpdate) -> dict[str, str]:
        """Enable or disable one tool (#213); the module keeps running, others unaffected."""
        await registry.set_tool_enabled(name, tool, body.enabled)
        return {"status": "ok"}

    @router.post("/{name}/tools/{tool}", response_model=ToolResult)
    async def invoke_tool(name: str, tool: str, request: ToolInvocation) -> ToolResult:
        return ToolResult(result=await registry.invoke(name, tool, request.arguments))

    @router.get("/{name}/status")
    async def get_module_status(name: str) -> dict[str, Any]:
        return await registry.get_status(name)

    @router.get("/{name}/docs")
    async def get_module_docs(name: str) -> dict[str, Any]:
        """Proxy the module's docs_url endpoint — the knowledge service fetches from here (#215)."""
        return await registry.get_docs(name)

    @router.get("/{name}/pages/{page_id}")
    async def get_module_page(request: Request, name: str, page_id: str) -> dict[str, Any]:
        # Forward all query params to the module so parameterised pages (e.g. a
        # calendar's start/end window, or the storage file browser's ?path= / ?q=)
        # work without the core needing to know each module's page-specific params.
        params = dict(request.query_params)
        return await registry.get_page(name, page_id, params=params or None)

    @router.get("/{name}/pages/{page_id}/doc")
    async def get_module_page_doc(name: str, page_id: str, path: str) -> dict[str, Any]:
        return await registry.get_page_doc(name, page_id, path)

    @router.get("/{name}/pages/{page_id}/doc/versions")
    async def get_module_page_doc_versions(name: str, page_id: str, path: str) -> dict[str, Any]:
        """List an editor document's saved versions, newest first (ADR-0046)."""
        return await registry.get_page_doc_versions(name, page_id, path)

    @router.get("/{name}/pages/{page_id}/doc/version")
    async def get_module_page_doc_version(
        name: str, page_id: str, path: str, version: str
    ) -> dict[str, Any]:
        """Fetch one past version of an editor document (ADR-0046)."""
        return await registry.get_page_doc_version(name, page_id, path, version)

    @router.put("/{name}/pages/{page_id}/doc")
    async def save_module_page_doc(
        name: str, page_id: str, path: str, body: DocSave
    ) -> dict[str, Any]:
        return await registry.save_page_doc(name, page_id, path, body.content)

    @router.post("/{name}/pages/{page_id}/folder")
    async def create_module_folder(name: str, page_id: str, path: str) -> dict[str, Any]:
        """Create a folder inside an editor page's store (#216)."""
        return await registry.create_page_folder(name, page_id, path)

    @router.delete("/{name}/pages/{page_id}/doc")
    async def delete_module_doc(name: str, page_id: str, path: str) -> Response:
        """Delete a document from an editor page's store (#216)."""
        await registry.delete_page_doc(name, page_id, path)
        return Response(status_code=204)

    @router.delete("/{name}/pages/{page_id}/folder")
    async def delete_module_folder(name: str, page_id: str, path: str) -> Response:
        """Delete an empty folder from an editor page's store (#216)."""
        await registry.delete_page_folder(name, page_id, path)
        return Response(status_code=204)

    @router.post("/{name}/pages/{page_id}/move")
    async def move_module_item(name: str, page_id: str, body: MoveRequest) -> dict[str, Any]:
        """Move or rename a file or folder within an editor page's store (#216)."""
        return await registry.move_page_item(name, page_id, body.from_path, body.to_path)

    @router.post("/{name}/pages/{page_id}/project")
    async def create_module_project(
        name: str, page_id: str, project: str = Query(...)
    ) -> dict[str, Any]:
        """Create a new knowledge base (project) in an editor page's store (#KB-refactor)."""
        return await registry.create_page_project(name, page_id, project)

    @router.delete("/{name}/pages/{page_id}/project")
    async def delete_module_project(name: str, page_id: str, project: str = Query(...)) -> Response:
        """Delete a knowledge base (project) and de-index its documents (#340)."""
        await registry.delete_page_project(name, page_id, project)
        return Response(status_code=204)

    @router.post("/{name}/pages/{page_id}/suggestions/{suggestion_id}/approve")
    async def approve_suggestion(
        name: str, page_id: str, suggestion_id: str, body: ApproveRequest | None = None
    ) -> dict[str, Any]:
        """Approve a staged suggestion: the module applies + indexes it (#220, ADR-0033).

        ``body.content`` (optional) is the operator's per-hunk-merged result (#KB-refactor).
        """
        return await registry.review_action(
            name, page_id, suggestion_id, "approve", content=body.content if body else None
        )

    @router.post("/{name}/pages/{page_id}/suggestions/{suggestion_id}/reject")
    async def reject_suggestion(name: str, page_id: str, suggestion_id: str) -> dict[str, Any]:
        """Reject a staged suggestion: the module discards it, vault untouched (#220)."""
        return await registry.review_action(name, page_id, suggestion_id, "reject")

    @router.post("/{name}/pages/{page_id}/send")
    async def send_mailbox_message(name: str, page_id: str, body: MailboxSend) -> dict[str, Any]:
        """Send an operator-composed mailbox message (ADR-0087) — compose or reply.

        The human-initiated counterpart to the agent draft confirm: the shell posts here when
        the operator presses Send on the mail page. Gated on the ``mailbox`` archetype and
        reachable only from the shell (never an MCP tool -> never the agent). Relays the
        module's own hint on a Gmail scope/rate-limit error.
        """
        return await registry.send_page_message(name, page_id, body.model_dump())

    @router.get("/{name}/pages/{page_id}/attachment")
    async def download_mailbox_attachment(
        name: str,
        page_id: str,
        message_id: str = Query(...),
        attachment_id: str = Query(...),
    ) -> StreamingResponse:
        """Stream a mailbox attachment from the module to the browser (ADR-0087).

        The core is the sole gateway browser <-> module; the bytes flow provider -> module ->
        here -> browser and are never stored. Gated on the ``mailbox`` archetype.
        """
        resp = await registry.download_page_attachment(name, page_id, message_id, attachment_id)
        content_type = resp.headers.get("content-type", "application/octet-stream")
        disposition = resp.headers.get("content-disposition", "")
        headers: dict[str, str] = {}
        if disposition:
            headers["content-disposition"] = disposition
        return StreamingResponse(resp.aiter_bytes(), media_type=content_type, headers=headers)

    @router.get("/{name}/download")
    async def download_module_file(name: str, path: str = Query(...)) -> StreamingResponse:
        """Proxy a binary file download from a module to the browser (ADR-0018).

        The core is the sole gateway between the browser and module internals;
        the browser never calls a module directly. ``path`` is forwarded as-is.
        """
        resp = await registry.download(name, path)
        content_type = resp.headers.get("content-type", "application/octet-stream")
        disposition = resp.headers.get("content-disposition", "")
        headers: dict[str, str] = {}
        if disposition:
            headers["content-disposition"] = disposition
        return StreamingResponse(
            resp.aiter_bytes(),
            media_type=content_type,
            headers=headers,
        )

    @router.get("/{name}/read")
    async def read_module_text(name: str, path: str = Query(...)) -> dict[str, Any]:
        """Read a text file's contents from a module for the split-screen reader (#KB-refactor)."""
        return await registry.read_text(name, path)

    @router.get("/{name}/resolve/{kind}/{ref_id}")
    async def resolve_entity(name: str, kind: str, ref_id: str) -> dict[str, Any]:
        return await registry.resolve_entity(name, kind, ref_id)

    @router.get("/{name}/attachments")
    async def list_attachments(name: str) -> list[dict[str, Any]]:
        return await registry.list_attachments(name)

    @router.get("/{name}/messages/{ref_id}")
    async def read_module_message(name: str, ref_id: str) -> dict[str, Any]:
        return await registry.read_message(name, ref_id)

    return router
