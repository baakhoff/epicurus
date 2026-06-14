"""The module registry — the core's view of installed modules (ADR-0004 / ADR-0007).

Discovers each configured module's manifest over the internal network and serves it
to the web shell: identity, tools, declared UI, health. Module config values
round-trip through the core into OpenBao (``modules/<name>/config``, tenant-scoped),
and manifest-declared UI actions invoke the module's MCP tools through the core —
the shell never talks to a module directly.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from epicurus_core import ModuleManifest, SecretError, SecretStore, get_logger
from epicurus_core_app.agent.mcp_host import McpHost

log = get_logger("epicurus_core_app.modules")


class ModuleStatus(BaseModel):
    """Liveness as seen from the core right now."""

    healthy: bool
    version: str | None = None


class ModuleSnapshot(BaseModel):
    """One installed module: its manifest plus current status."""

    manifest: ModuleManifest
    status: ModuleStatus


class ToolInvocation(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    result: str


class ModuleRegistry:
    """Fetches module manifests/health and routes UI actions to module tools."""

    def __init__(
        self, base_urls: list[str], *, mcp: McpHost, secrets: SecretStore, tenant: str
    ) -> None:
        self._bases = list(base_urls)
        self._mcp = mcp
        self._secrets = secrets
        self._tenant = tenant

    async def snapshot(self) -> list[ModuleSnapshot]:
        """Every configured module — reachable ones with their manifest, dead ones flagged."""
        return list(await asyncio.gather(*(self._probe(base) for base in self._bases)))

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

    async def invoke(self, name: str, tool: str, arguments: dict[str, Any]) -> str:
        """Run a module tool (a manifest-declared UI action) through the MCP host."""
        base, manifest = await self._resolve(name)
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

    async def get_page(
        self, name: str, page_id: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Proxy a module's page-data endpoint to the shell (ADR-0018).

        The page must be declared in the module's ``manifest.pages``; the core then
        fetches ``GET /pages/{page_id}`` on the module and returns its JSON body —
        the archetype's data shape, which the shell renders. A module never serves
        UI markup. Returns 404 if the module is unreachable or declares no such page.
        Query params (e.g. ``path``, ``q``) are forwarded to the module as-is.
        """
        base, manifest = await self._resolve(name)
        if page_id not in {p.id for p in manifest.pages}:
            raise HTTPException(status_code=404, detail=f"module {name!r} has no page {page_id!r}")
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.get(f"/pages/{page_id}", params=params if params else None)
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
        base, manifest = await self._resolve(name)
        if not manifest.attachable:
            raise HTTPException(status_code=404, detail=f"module {name!r} is not attachable")
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            resp = await client.get(f"/attachments/{ref_id}")
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data


def create_modules_router(registry: ModuleRegistry) -> APIRouter:
    """The module surface the web shell renders (list, config, actions)."""
    router = APIRouter(prefix="/platform/v1/modules", tags=["modules"])

    @router.get("", response_model=list[ModuleSnapshot])
    async def list_modules() -> list[ModuleSnapshot]:
        return await registry.snapshot()

    @router.get("/{name}/config")
    async def get_config(name: str) -> dict[str, Any]:
        return await registry.get_config(name)

    @router.put("/{name}/config")
    async def set_config(name: str, values: dict[str, Any]) -> dict[str, str]:
        await registry.set_config(name, values)
        return {"status": "ok"}

    @router.post("/{name}/tools/{tool}", response_model=ToolResult)
    async def invoke_tool(name: str, tool: str, request: ToolInvocation) -> ToolResult:
        return ToolResult(result=await registry.invoke(name, tool, request.arguments))

    @router.get("/{name}/status")
    async def get_module_status(name: str) -> dict[str, Any]:
        return await registry.get_status(name)

    @router.get("/{name}/pages/{page_id}")
    async def get_module_page(request: Request, name: str, page_id: str) -> dict[str, Any]:
        # Forward all query params to the module so parameterised pages (e.g.
        # the storage file browser's ?path= / ?q=) work without the core
        # needing to know about each module's page-specific params.
        params = dict(request.query_params)
        return await registry.get_page(name, page_id, params=params or None)

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

    return router
