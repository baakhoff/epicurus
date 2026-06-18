"""The module registry — the core's view of installed modules (ADR-0004 / ADR-0007).

Discovers each configured module's manifest over the internal network and serves it
to the web shell: identity, tools, declared UI, health. Module config values
round-trip through the core into OpenBao (``modules/<name>/config``, tenant-scoped),
and manifest-declared UI actions invoke the module's MCP tools through the core —
the shell never talks to a module directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from epicurus_core import ModuleManifest, SecretError, SecretStore, get_logger
from epicurus_core_app.agent.mcp_host import McpHost
from epicurus_core_app.docker_control import DockerController, DockerError
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


class ModuleRegistry:
    """Fetches module manifests/health and routes UI actions to module tools."""

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

    async def snapshot(self) -> list[ModuleSnapshot]:
        """Every configured module — reachable ones with their manifest, dead ones flagged.

        Each snapshot carries the operator's ``enabled`` flag (#126) and a ``removed``
        tombstone (#127); unset defaults to enabled and not-removed. The list stays **1:1
        with the configured bases** (``_resolve`` / ``enabled_mcp_urls`` zip it against them),
        so disabled and removed modules are flagged in place, not dropped — disabled ones
        are still shown so the shell can re-enable them, while removed ones are dropped by
        the list endpoint.
        """
        probed = await asyncio.gather(*(self._probe(base) for base in self._bases))
        enabled = await self._prefs.enabled_map(self._tenant)
        removed = await self._prefs.removed_modules(self._tenant)
        for snap in probed:
            snap.enabled = enabled.get(snap.manifest.name, True)
            snap.removed = snap.manifest.name in removed
            snap.disabled_tools = sorted(
                await self._prefs.get_disabled_tools(self._tenant, snap.manifest.name)
            )
        return list(probed)

    async def _probe(self, base: str) -> ModuleSnapshot:
        try:
            async with httpx.AsyncClient(base_url=base, timeout=5) as client:
                manifest_resp = await client.get("/manifest")
                manifest_resp.raise_for_status()
                manifest = ModuleManifest.model_validate(manifest_resp.json())
                health_resp = await client.get("/health")
                healthy = health_resp.status_code == 200
                version = (health_resp.json() or {}).get("version") if healthy else None
            return ModuleSnapshot(
                manifest=manifest, status=ModuleStatus(healthy=healthy, version=version)
            )
        except Exception as exc:  # a dead module is a fact to display, not an error
            log.warning("module probe failed", base=base, error=str(exc))
            name = urlsplit(base).hostname or base
            return ModuleSnapshot(
                manifest=ModuleManifest(name=name, version="unknown"),
                status=ModuleStatus(healthy=False),
            )

    async def _resolve(self, name: str) -> tuple[str, ModuleManifest]:
        """The base URL + manifest of the module called ``name`` (404 if absent)."""
        for snapshot, base in zip(await self.snapshot(), self._bases, strict=True):
            if snapshot.manifest.name == name and snapshot.status.healthy:
                return base, snapshot.manifest
        raise HTTPException(status_code=404, detail=f"no reachable module named {name!r}")

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
        """Stop + remove a module's container, then tombstone it (#127, ADR-0028).

        Privileged and confirmed in the UI. 404 for an unknown module, 403 for a
        protected/core service (also enforced in the Docker layer), 503 when the Docker
        socket is unavailable. Idempotent: a module whose container is already gone still
        tombstones. The tombstone hides the module everywhere and is re-enforced on the
        next startup, so a ``compose up`` cannot silently resurrect it.
        """
        live = {snap.manifest.name for snap in await self.snapshot() if not snap.removed}
        if name not in live:
            raise HTTPException(status_code=404, detail=f"no module named {name!r}")
        if self._docker is None:
            raise HTTPException(
                status_code=503, detail="module removal unavailable: the core has no Docker access"
            )
        try:
            containers = await asyncio.to_thread(self._docker.remove_module, name)
        except DockerError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        await self._prefs.set_removed(self._tenant, name, True)
        log.info("module removed", module=name, containers=containers)
        return {"removed": name, "containers": containers}

    async def reconcile_tombstones(self) -> None:
        """Re-remove any tombstoned module whose container has reappeared (#127).

        Run at startup so a removal survives a ``compose up`` / Watchtower pull. Best-effort
        — a missing socket or a transient Docker error is logged, never fatal.
        """
        if self._docker is None:
            return
        for name in await self._prefs.removed_modules(self._tenant):
            try:
                containers = await asyncio.to_thread(self._docker.remove_module, name)
                if containers:
                    log.info("re-removed resurrected module", module=name, containers=containers)
            except Exception as exc:
                log.warning("tombstone reconcile failed", module=name, error=str(exc))

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
        """Run a module tool (a manifest-declared UI action) through the MCP host."""
        base, manifest = await self._resolve(name)
        if not await self._prefs.is_enabled(self._tenant, name):
            raise HTTPException(status_code=403, detail=f"module {name!r} is disabled")
        if tool not in {t.name for t in manifest.tools}:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no tool {tool!r}")
        return await self._mcp.call(tool, arguments, f"{base}/mcp")

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

    async def get_status(self, name: str) -> dict[str, Any]:
        """Proxy the module's declared ``status_url`` endpoint to the caller.

        The module's manifest must declare ``ui.status_url`` (e.g. ``/status``);
        the core fetches that path on the module and returns the JSON body.
        Returns 404 if the module is unreachable or has no ``status_url``.
        """
        base, manifest = await self._resolve(name)
        status_url = manifest.ui.status_url if manifest.ui else None
        if not status_url:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no status_url")
        async with httpx.AsyncClient(base_url=base, timeout=5) as client:
            resp = await client.get(status_url)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
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
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.get(manifest.docs_url)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
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
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.get(f"/pages/{page_id}", params=dict(params or {}))
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
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

    async def get_page_doc(self, name: str, page_id: str, path: str) -> dict[str, Any]:
        """Proxy a single editor document's content to the shell (ADR-0018).

        The shell fetches ``GET /pages/{page_id}/doc?path=<path>`` on the module and
        returns its ``{path, title, content}`` body. ``path`` is module-relative; the
        module is responsible for confining it (no traversal out of its store).
        """
        base = await self._resolve_editor_page(name, page_id)
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.get(f"/pages/{page_id}/doc", params={"path": path})
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
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
        async with httpx.AsyncClient(base_url=base, timeout=60) as client:
            resp = await client.put(
                f"/pages/{page_id}/doc", params={"path": path}, json={"content": content}
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

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
        if the destination already exists, 404 if the source does not exist.
        """
        base = await self._resolve_editor_page(name, page_id)
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.post(
                f"/pages/{page_id}/move",
                json={"from_path": from_path, "to_path": to_path},
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    async def download(self, name: str, path: str) -> httpx.Response:
        """Proxy a binary file download from a module's ``/download`` endpoint.

        The module must be reachable; ``path`` is forwarded as-is. The caller is
        responsible for streaming the response body. 404 if the module is unreachable.
        """
        base, _ = await self._resolve(name)
        client = httpx.AsyncClient(base_url=base, timeout=60)
        resp = await client.get("/download", params={"path": path})
        resp.raise_for_status()
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
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.get(f"/resolve/{kind}/{ref_id}")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    async def list_attachments(self, name: str) -> list[dict[str, Any]]:
        """Proxy a module's attachment picker (ADR-0019): ``GET /attachments``.

        The manifest must set ``attachable`` true; returns the module's attachable items
        (each ``{ref_id, kind, title}``). 404 if unreachable or not attachable.
        """
        base, manifest = await self._resolve(name)
        if not manifest.attachable:
            raise HTTPException(status_code=404, detail=f"module {name!r} is not attachable")
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.get("/attachments")
            resp.raise_for_status()
            items: list[dict[str, Any]] = resp.json()
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
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.get(f"/attachments/{ref_id}")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

    async def read_message(self, name: str, ref_id: str) -> dict[str, Any]:
        """Proxy a module's full-message endpoint to the panel (ADR-0019).

        Fetches ``GET /messages/{ref_id}`` on the module and returns the
        EmailMessage envelope — subject, from, date, and body — consumed by the
        right-panel ``email-reader`` view. 404 if the module is unreachable.
        """
        _safe_segment(ref_id, label="ref_id")
        base, _ = await self._resolve(name)
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.get(f"/messages/{ref_id}")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data


def create_modules_router(registry: ModuleRegistry) -> APIRouter:
    """The module surface the web shell renders (list, config, actions)."""
    router = APIRouter(prefix="/platform/v1/modules", tags=["modules"])

    @router.get("", response_model=list[ModuleSnapshot])
    async def list_modules() -> list[ModuleSnapshot]:
        # Drop tombstoned modules — a removed module is gone, not merely disabled (#127).
        return [snap for snap in await registry.snapshot() if not snap.removed]

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
        """Confirmed module removal (#127): stop + remove the container, then tombstone it.

        Privileged — gated by a confirm dialog in the shell. 403 for a protected service,
        503 when the core has no Docker access, 404 for an unknown module.
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
