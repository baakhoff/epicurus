"""The core-owned file-space platform API (ADR-0052 / ADR-0063), under ``/platform/v1/files``.

Two surfaces share one router and one swappable :class:`~epicurus_core.files.FileStore`
(local-FS ↔ S3, constraint #3); tenant scoping (constraint #1) is enforced on every call:

* **Module-facing I/O** (Phase 1, ADR-0052) — ``list`` / ``read`` / ``write`` / ``stat`` /
  ``delete`` / ``dir`` / ``move``. Modules consume these via ``PlatformClient.files_*`` instead
  of mounting the shared volume.
* **Operator-facing Files UI** (Phase 2, ADR-0063) — ``page`` (the browser archetype's data),
  ``search``, and ``download``. The Files page used to live in the storage module; it now lives
  here, served from the core-owned file index over the FileStore, **merged** with the storage
  module's object store (chat uploads / agent objects) via an injected :class:`ObjectBackend`.
  ``read`` / ``move`` / ``download`` are file-space-first and fall back to the object store, so
  the core is the single front door for the whole Files view.
"""

from __future__ import annotations

import mimetypes
from contextlib import suppress
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from epicurus_core import FileEntry, FileStore
from epicurus_core.files import normalize_rel
from epicurus_core.tenancy import TenantError, validate_tenant_id
from epicurus_core_app.file_index import FileIndex
from epicurus_core_app.object_backend import ObjectBackend


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


class FileSearchResponse(BaseModel):
    """Name/path search hits over the core file index."""

    entries: list[FileEntry]


def _fmt_size(size: int) -> str:
    """Human-readable file size."""
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if size >= threshold:
            return f"{size / threshold:.1f} {unit}"
    return f"{size} B"


_DOWNLOAD_BASE = "/platform/v1/files/download"


def _browser_item(
    *, path: str, name: str, kind: str, size: int, movable: bool
) -> dict[str, object]:
    """Shape one ``BrowserItem`` (ADR-0018) for the Files page."""
    is_dir = kind == "dir"
    return {
        "id": path,
        "title": name,
        "subtitle": "directory" if is_dir else _fmt_size(size),
        "body": None,
        "icon": "folder" if is_dir else "file",
        "nav_path": path if is_dir else None,
        "href": f"{_DOWNLOAD_BASE}?path={quote(path)}" if not is_dir else None,
        "movable": movable,
    }


def _disposition(name: str) -> str:
    """A ``Content-Disposition`` header that downloads as *name* (header-safe)."""
    safe = name.replace('"', "").replace("\\", "").replace("\r", "").replace("\n", "")
    return f'attachment; filename="{safe}"'


def create_files_router(
    store: FileStore,
    *,
    default_tenant: str = "local",
    index: FileIndex | None = None,
    objects: ObjectBackend | None = None,
) -> APIRouter:
    """Build the ``/platform/v1/files`` router over a :class:`FileStore`.

    *index* powers the Files page's search (the file-space tree, name/path matched); without it
    search is empty. *objects* merges the storage module's object store into the Files view and
    serves object read/download/move; without it the view is the file-space tree alone.
    """
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

    # ── Module-facing I/O (Phase 1) ──────────────────────────────────────────────

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
        except FileNotFoundError:
            # Not in the file space — it may be an object-store entry (upload / agent object).
            if objects is not None:
                obj = await objects.read(tenant=tenant, path=rel)
                if obj is not None:
                    return FileReadResponse(path=obj.path, name=obj.name, content=obj.content)
            raise HTTPException(status_code=404, detail="not found") from None
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
            moved = await store.move(tenant=tenant, src=body.src, dst=body.dst)
        except FileNotFoundError:
            # Not a file-space entry — try the object store (a movable upload/agent object).
            if objects is not None:
                entry = await objects.move(tenant=tenant, src=body.src, dst=body.dst)
                return FileEntry(path=entry.path, name=entry.name, kind=entry.kind, size=entry.size)
            raise HTTPException(status_code=404, detail="source not found") from None
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="destination already exists") from exc
        except ValueError as exc:  # tenant root, or a move into the path itself
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Keep the index in step immediately so a moved file is searchable at once (the watcher
        # would also catch it, debounced). Best-effort: the move itself already succeeded.
        if index is not None:
            with suppress(Exception):  # index freshness is best-effort; the move already stands
                await index.upsert_batch(
                    tenant=tenant,
                    entries=[
                        {
                            "path": moved.path,
                            "name": moved.name,
                            "size": moved.size,
                            "mtime": moved.mtime,
                            "kind": moved.kind,
                        }
                    ],
                )
        return moved

    # ── Operator-facing Files UI (Phase 2) ───────────────────────────────────────

    @router.get("/page")
    async def files_page(
        path: str = Query(default="", description="Directory to browse (empty = root)"),
        q: str = Query(default="", description="Search query; if set, overrides path browsing"),
        tenant_id: str | None = Query(default=None),
    ) -> dict[str, object]:
        """Serve the Files browser page data (ADR-0018) over the unified file space.

        Returns a ``BrowserData``-shaped payload: ``{title, path, search_enabled, items}``.
        Merges the file-space tree (folders the user navigates, files they read/download) with
        the storage module's objects. File-space entries are read-only in the UI (modules own
        their subtrees); only object entries (uploads / agent-written) are ``movable``.
        """
        tenant = _tenant(tenant_id)
        query = q.strip()
        items: list[dict[str, object]] = []
        if query:
            fs_hits = await index.search(tenant=tenant, query=query, limit=200) if index else []
            for e in fs_hits:
                items.append(
                    _browser_item(path=e.path, name=e.name, kind=e.kind, size=e.size, movable=False)
                )
            title = f"Files — {query}"
        else:
            rel = _safe(path)
            for fe in await store.list_dir(tenant=tenant, path=rel):
                items.append(
                    _browser_item(
                        path=fe.path, name=fe.name, kind=fe.kind, size=fe.size, movable=False
                    )
                )
            title = f"Files — {path}" if path else "Files"

        if objects is not None:
            for oe in await objects.list(tenant=tenant, path=path, query=query):
                items.append(
                    _browser_item(
                        path=oe.path, name=oe.name, kind=oe.kind, size=oe.size, movable=True
                    )
                )

        # Dirs before files, then by name — the merged view reads like one tree.
        items.sort(key=lambda it: (it["icon"] != "folder", str(it["title"]).lower()))
        return {"title": title, "path": path, "search_enabled": True, "items": items}

    @router.get("/search", response_model=FileSearchResponse)
    async def search_files(
        q: str = Query(..., description="Name/path fragment to match"),
        limit: int = Query(default=50, ge=1, le=200),
        tenant_id: str | None = Query(default=None),
    ) -> FileSearchResponse:
        """Search the core file index by name/path fragment (backs ``files_search``)."""
        tenant = _tenant(tenant_id)
        if index is None or not q.strip():
            return FileSearchResponse(entries=[])
        hits = await index.search(tenant=tenant, query=q.strip(), limit=limit)
        return FileSearchResponse(
            entries=[
                FileEntry(path=h.path, name=h.name, kind=h.kind, size=h.size, mtime=h.mtime)
                for h in hits
            ]
        )

    @router.get("/download")
    async def download_file(
        path: str = Query(..., description="File to download, relative to the tenant root"),
        tenant_id: str | None = Query(default=None),
    ) -> Response:
        """Stream a file from the unified file space — file-space first, then the object store."""
        tenant = _tenant(tenant_id)
        rel = _safe(path)
        entry = await store.stat(tenant=tenant, path=rel)
        if entry is not None and entry.kind == "file":
            data = await store.read_bytes(tenant=tenant, path=rel)
            media = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
            return Response(
                content=data,
                media_type=media,
                headers={"content-disposition": _disposition(entry.name)},
            )
        if objects is not None:
            dl = await objects.download(tenant=tenant, path=rel)
            if dl is not None:
                return StreamingResponse(
                    dl.body,
                    media_type=dl.content_type,
                    headers={"content-disposition": _disposition(dl.name)},
                )
        raise HTTPException(status_code=404, detail="not found")

    return router
