"""Bridge to the storage module's object store for the core Files view (ADR-0063).

Phase 2 of the file-space migration (ADR-0052) moves the **Files UI** into the core, but the
storage module keeps owning its MinIO object store — the chat-upload sink and agent-written
objects. So the core's unified Files page is the file-space tree (which the core owns, indexed
over the :class:`~epicurus_core.files.FileStore`) **plus** those objects, merged in live.

This module is the seam: an :class:`ObjectBackend` protocol the file router depends on, and a
:class:`StorageObjectBackend` that fulfils it by proxying to the storage module through the
existing :class:`ModuleRegistry` (the same health-gated, local-only path the page/download
proxies already use — constraint #7). The core never touches MinIO directly; storage stays the
sole owner of the object bucket. If storage is down, listing degrades to an empty set (the page
still shows the file-space tree) and a read/download/move of an object surfaces a clean error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

import httpx
from fastapi import HTTPException
from pydantic import BaseModel

from epicurus_core import get_logger
from epicurus_core.files import FileKind
from epicurus_core_app.modules import ModuleRegistry

log = get_logger("core.object_backend")

_STORAGE = "storage"


class ObjectEntry(BaseModel):
    """One object-store entry, in the minimal shape the Files view needs."""

    path: str
    name: str
    size: int = 0
    mtime: float = 0.0
    kind: FileKind = "file"


class ObjectText(BaseModel):
    """A text object's contents for the split-screen reader."""

    path: str
    name: str
    content: str


class ObjectDownload(BaseModel):
    """A streaming object download — the bytes plus how to present them."""

    model_config = {"arbitrary_types_allowed": True}

    name: str
    content_type: str
    body: AsyncIterator[bytes]


class ObjectBackend(Protocol):
    """The object-store surface the core Files view consumes (fulfilled by storage)."""

    async def list(self, *, tenant: str, path: str, query: str) -> list[ObjectEntry]:
        """Object entries under *path* (or matching *query* if set). Empty if unavailable."""

    async def read(self, *, tenant: str, path: str) -> ObjectText | None:
        """Text at *path*, or ``None`` if it is not a (readable) object."""

    async def download(self, *, tenant: str, path: str) -> ObjectDownload | None:
        """Stream the object at *path*, or ``None`` if it is not an object."""

    async def move(self, *, tenant: str, src: str, dst: str) -> ObjectEntry:
        """Move/rename the object at *src* to *dst*; raise ``HTTPException`` on failure."""


class StorageObjectBackend:
    """An :class:`ObjectBackend` backed by the storage module via the registry proxy.

    Resolution is health-gated (the registry only returns a reachable, enabled storage), so a
    stopped or absent storage module makes ``list`` degrade to ``[]`` — the Files page still
    renders the file-space tree — while a read/download/move raises a clean error. The core
    reaches storage only over the internal module network (constraint #7).
    """

    def __init__(self, registry: ModuleRegistry, *, timeout: float = 30.0) -> None:
        self._registry = registry
        self._timeout = timeout

    async def _base(self, tenant: str) -> str | None:
        """The reachable storage base URL, or ``None`` if storage is unavailable."""
        try:
            return await self._registry.base_url(_STORAGE)
        except HTTPException:
            return None

    def _params(self, tenant: str, **extra: str) -> dict[str, str]:
        return {"tenant_id": tenant, **extra}

    async def list(self, *, tenant: str, path: str, query: str) -> list[ObjectEntry]:
        base = await self._base(tenant)
        if base is None:
            return []
        try:
            async with httpx.AsyncClient(base_url=base, timeout=self._timeout) as http:
                resp = await http.get("/objects", params=self._params(tenant, path=path, q=query))
                resp.raise_for_status()
                return [ObjectEntry.model_validate(e) for e in resp.json()["entries"]]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            # The object store is best-effort for the *view*: never let it blank the page.
            log.warning("object listing failed; showing file-space tree only", error=str(exc))
            return []

    async def read(self, *, tenant: str, path: str) -> ObjectText | None:
        base = await self._base(tenant)
        if base is None:
            return None
        async with httpx.AsyncClient(base_url=base, timeout=self._timeout) as http:
            resp = await http.get("/objects/read", params=self._params(tenant, path=path))
            if resp.status_code == 404:
                return None
            if resp.status_code in (413, 415):
                raise HTTPException(status_code=resp.status_code, detail=resp.json().get("detail"))
            resp.raise_for_status()
            return ObjectText.model_validate(resp.json())

    async def download(self, *, tenant: str, path: str) -> ObjectDownload | None:
        base = await self._base(tenant)
        if base is None:
            return None
        client = httpx.AsyncClient(base_url=base, timeout=60.0)
        try:
            # Storage serves object bytes at /download (the platform module-download convention,
            # the same endpoint the registry's generic download proxy uses), not /objects/download.
            req = client.build_request("GET", "/download", params=self._params(tenant, path=path))
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(status_code=502, detail="object download failed") from exc
        if resp.status_code == 404:
            await resp.aclose()
            await client.aclose()
            return None
        if resp.status_code != 200:
            await resp.aclose()
            await client.aclose()
            raise HTTPException(
                status_code=502, detail=f"object download returned {resp.status_code}"
            )

        async def _stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return ObjectDownload(
            name=path.rsplit("/", 1)[-1],
            content_type=resp.headers.get("content-type", "application/octet-stream"),
            body=_stream(),
        )

    async def move(self, *, tenant: str, src: str, dst: str) -> ObjectEntry:
        base = await self._base(tenant)
        if base is None:
            raise HTTPException(status_code=502, detail="storage module unavailable")
        async with httpx.AsyncClient(base_url=base, timeout=self._timeout) as http:
            resp = await http.post(
                "/objects/move",
                params=self._params(tenant),
                json={"from_path": src, "to_path": dst},
            )
            if 400 <= resp.status_code < 500:
                raise HTTPException(status_code=resp.status_code, detail=resp.json().get("detail"))
            resp.raise_for_status()
            # The move contract returns just the new path; shape it as the moved entry.
            new_path = str(resp.json()["path"])
            return ObjectEntry(path=new_path, name=new_path.rsplit("/", 1)[-1])
