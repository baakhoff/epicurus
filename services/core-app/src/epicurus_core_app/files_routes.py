"""The core-owned file-space platform API (ADR-0052), under ``/platform/v1/files``.

The core owns the per-tenant user file space; modules read and write it through these
endpoints — via :meth:`PlatformClient.files_list` / ``files_read`` / ``files_write`` /
``files_stat`` / ``files_delete`` / ``files_make_dir`` — instead of mounting the shared
volume and doing their own I/O. Backed by a swappable :class:`~epicurus_core.files.FileStore`
(local-FS ↔ S3, constraint #3); tenant scoping (constraint #1) is enforced on every call.

This is Phase 1: the contract + backend. Migrating storage/knowledge/notes off their direct
mounts onto this API, and serving the Files browser from here, is staged follow-up work.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from epicurus_core import FileEntry, FileStore
from epicurus_core.files import normalize_rel
from epicurus_core.tenancy import TenantError, validate_tenant_id


class FileListResponse(BaseModel):
    """Direct children of a directory in the tenant file space."""

    entries: list[FileEntry]


class FileReadResponse(BaseModel):
    """A text file's contents for inline reads."""

    path: str
    name: str
    content: str


class FileWriteBody(BaseModel):
    """Request body for ``PUT /platform/v1/files/write``."""

    content: str


class FileDeleteResponse(BaseModel):
    """Whether the deleted path existed."""

    deleted: bool


class FileMoveBody(BaseModel):
    """Request body for ``POST /platform/v1/files/move`` — rename is a same-parent move."""

    src: str
    dst: str


def create_files_router(store: FileStore, *, default_tenant: str = "local") -> APIRouter:
    """Build the ``/platform/v1/files`` router over a :class:`FileStore`."""
    router = APIRouter(prefix="/platform/v1/files", tags=["files"])

    def _tenant(tenant_id: str | None) -> str:
        try:
            return validate_tenant_id(tenant_id or default_tenant)
        except TenantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _safe(path: str) -> str:
        """Validate the path up front so traversal is a clean 400 (not a store error)."""
        try:
            return normalize_rel(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/list", response_model=FileListResponse)
    async def list_files(
        path: str = Query(default="", description="Directory to list (empty = tenant root)"),
        tenant_id: str | None = Query(default=None),
    ) -> FileListResponse:
        tenant = _tenant(tenant_id)
        _safe(path)
        return FileListResponse(entries=await store.list_dir(tenant=tenant, path=path))

    @router.get("/read", response_model=FileReadResponse)
    async def read_file(
        path: str = Query(..., description="File to read, relative to the tenant root"),
        tenant_id: str | None = Query(default=None),
    ) -> FileReadResponse:
        tenant = _tenant(tenant_id)
        rel = _safe(path)
        try:
            content = await store.read_text(tenant=tenant, path=path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="not found") from exc
        except ValueError as exc:  # the 256 KB text cap (traversal already handled by _safe)
            raise HTTPException(
                status_code=413, detail="file is too large to read as text"
            ) from exc
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="file is not UTF-8 text") from exc
        return FileReadResponse(path=rel, name=rel.rsplit("/", 1)[-1], content=content)

    @router.get("/stat", response_model=FileEntry)
    async def stat_file(
        path: str = Query(...),
        tenant_id: str | None = Query(default=None),
    ) -> FileEntry:
        tenant = _tenant(tenant_id)
        _safe(path)
        entry = await store.stat(tenant=tenant, path=path)
        if entry is None:
            raise HTTPException(status_code=404, detail="not found")
        return entry

    @router.put("/write", response_model=FileEntry)
    async def write_file(
        body: FileWriteBody,
        path: str = Query(...),
        tenant_id: str | None = Query(default=None),
    ) -> FileEntry:
        tenant = _tenant(tenant_id)
        _safe(path)
        try:
            return await store.write_text(tenant=tenant, path=path, content=body.content)
        except ValueError as exc:  # e.g. writing to the tenant root itself
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("", response_model=FileDeleteResponse)
    async def delete_file(
        path: str = Query(...),
        tenant_id: str | None = Query(default=None),
    ) -> FileDeleteResponse:
        tenant = _tenant(tenant_id)
        _safe(path)
        try:
            deleted = await store.delete(tenant=tenant, path=path)
        except ValueError as exc:  # deleting the tenant root is rejected
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileDeleteResponse(deleted=deleted)

    @router.post("/dir", response_model=FileEntry)
    async def make_dir(
        path: str = Query(...),
        tenant_id: str | None = Query(default=None),
    ) -> FileEntry:
        tenant = _tenant(tenant_id)
        _safe(path)
        return await store.ensure_dir(tenant=tenant, path=path)

    @router.post("/move", response_model=FileEntry)
    async def move_file(
        body: FileMoveBody,
        tenant_id: str | None = Query(default=None),
    ) -> FileEntry:
        tenant = _tenant(tenant_id)
        _safe(body.src)
        _safe(body.dst)
        try:
            return await store.move(tenant=tenant, src=body.src, dst=body.dst)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="source not found") from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="destination already exists") from exc
        except ValueError as exc:  # tenant root, or a move into the path itself
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
