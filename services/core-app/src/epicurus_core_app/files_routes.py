"""The core-owned file-space platform API (ADR-0052 / ADR-0063), under ``/platform/v1/files``.

Two surfaces share one router and one swappable :class:`~epicurus_core.files.FileStore`
(local-FS ↔ S3, constraint #3); tenant scoping (constraint #1) is enforced on every call:

* **Module-facing I/O** (Phase 1, ADR-0052) — ``list`` / ``read`` / ``write`` / ``stat`` /
  ``delete`` / ``dir`` / ``move``. Modules consume these via ``PlatformClient.files_*`` instead
  of mounting the shared volume.
* **Operator-facing Files UI** (Phase 2, ADR-0063) — ``page`` (the browser archetype's data),
  ``search``, ``download``, ``upload`` (#479, the Files page's way in — bounded by the shared
  #175 caps), and ``delete`` (#564, the Files page's way out). The Files page used to live in
  the storage module; it now lives here, served from the core-owned file index over the
  FileStore, **merged** with the storage module's object store (chat uploads / agent objects)
  via an injected :class:`ObjectBackend`. ``read`` / ``move`` / ``download`` / ``delete`` are
  file-space-first and fall back to the object store, so the core is the single front door for
  the whole Files view.

The operator ``upload`` and ``delete`` doors mirror the module-facing ``write`` and ``DELETE``:
same underlying seam, but the operator doors add the #479 ownership guard (a module owns its
top-level subtree; that lifecycle belongs to its own page) which the module doors must not have
— modules write and delete *inside* their own subtrees through the platform contract.

**External mounts** (#731): a ``path`` prefixed ``mount:<name>/<sub-path>`` (see
:mod:`epicurus_core_app.mounts`) is transparently redirected to that mount's own store by
:func:`_target` — every handler below resolves through it before touching *any* store, so
the tenant-space code paths run unchanged when no prefix is present. A mount's read-only flag
is enforced here (not just hidden in the UI) via :func:`_require_writable`.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from epicurus_core import FileEntry, FileStore
from epicurus_core.files import PathEscapeError, normalize_rel
from epicurus_core.tenancy import TenantError, validate_tenant_id
from epicurus_core_app.core_events import CoreEventEmitter
from epicurus_core_app.file_index import FileIndex
from epicurus_core_app.file_scan import ScanFuse, namespace_for
from epicurus_core_app.mounts import MOUNT_PREFIX, Mount
from epicurus_core_app.object_backend import ObjectBackend
from epicurus_core_app.upload_limits import (
    DEFAULT_ALLOWED_UPLOAD_TYPES,
    DEFAULT_MAX_UPLOAD_BYTES,
    content_type_allowed,
)


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


class ScanFuseState(BaseModel):
    """One namespace whose index purge the mass de-index fuse is refusing (#848)."""

    tenant: str
    namespace: str
    indexed_rows: int
    would_delete: int
    seen_entries: int
    reason: str
    at: str


class ScanStatusResponse(BaseModel):
    """Whether any file-space scan is currently refusing to purge index rows (#848)."""

    fuse_enabled: bool
    max_delete_ratio: float
    min_deletions: int
    tripped: bool
    namespaces: list[ScanFuseState]


class RescanResponse(BaseModel):
    """The outcome of an on-demand rescan (#848)."""

    namespace: str
    entries: int
    forced: bool
    tripped: bool


def _fmt_size(size: int) -> str:
    """Human-readable file size."""
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if size >= threshold:
            return f"{size / threshold:.1f} {unit}"
    return f"{size} B"


_DOWNLOAD_BASE = "/platform/v1/files/download"


def _browser_item(
    *, path: str, name: str, kind: str, size: int, movable: bool, deletable: bool
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
        "deletable": deletable,
    }


_MAX_SEGMENT_BYTES = 255


def _reject_pathological(path: str) -> None:
    """400 on a name the store would otherwise only reject deep down with a 500 (#554).

    ``normalize_rel`` blocks traversal but not a control char / NUL or an over-long segment
    (>255 bytes is the common filesystem limit); those surface as an ``OSError`` inside the
    FileStore. Turn them into a clean 400 at the door — for both upload and move/rename — so a
    bad name is a client error, not a server error.
    """
    for seg in path.split("/"):
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in seg):
            raise HTTPException(status_code=400, detail="name contains control characters")
        if len(seg.encode("utf-8")) > _MAX_SEGMENT_BYTES:
            raise HTTPException(status_code=400, detail="name segment exceeds 255 bytes")


def _dedupe_key(kind: str, path: str) -> tuple[str, str]:
    """The identity of one browser row across the two listing sources (#560).

    ``files_page`` merges the file-space tree with the object store; a folder (or file) present
    in both would otherwise render twice. :func:`normalize_rel` is the single string that
    addresses a node across the backend, the index, and the object store (see
    ``epicurus_core.files``), so the same node from either source collapses to one key. A store
    never emits a ``..`` path, but fall back to the raw path rather than 500 the listing if one
    ever did.
    """
    try:
        return (kind, normalize_rel(path))
    except ValueError:
        return (kind, path)


def _disposition(name: str) -> str:
    """A ``Content-Disposition`` header that downloads as *name* (header-safe)."""
    safe = name.replace('"', "").replace("\\", "").replace("\r", "").replace("\n", "")
    return f'attachment; filename="{safe}"'


def _not_found_detail(path: str) -> str:
    """A 404 ``detail`` that names the path and suggests a recovery step (#742) — for an agent
    caller (directly, or via a module tool that forwards this text), a bare ``"not found"``
    gives nothing to act on, while this names exactly what to re-check and how."""
    return (
        f'"{path}" does not exist — it may have been deleted or moved; '
        "list the folder for current contents"
    )


def create_files_router(
    store: FileStore,
    *,
    default_tenant: str = "local",
    index: FileIndex | None = None,
    objects: ObjectBackend | None = None,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    allowed_upload_types: Sequence[str] = DEFAULT_ALLOWED_UPLOAD_TYPES,
    locked_prefixes: frozenset[str] = frozenset(),
    events: CoreEventEmitter | None = None,
    mounts: Mapping[str, Mount] | None = None,
    scan_fuse: ScanFuse | None = None,
    rescan: Callable[..., Awaitable[int]] | None = None,
) -> APIRouter:
    """Build the ``/platform/v1/files`` router over a :class:`FileStore`.

    *index* powers the Files page's search (the file-space tree, name/path matched); without it
    search is empty. *objects* merges the storage module's object store into the Files view and
    serves object read/download/move; without it the view is the file-space tree alone.
    ``max_upload_bytes`` / ``allowed_upload_types`` bound the upload route (the shared #175
    caps). *locked_prefixes* names the top-level folders modules own (by convention their
    hostnames, ADR-0063): files under them are not movable in the UI and not upload targets.
    *events* announces mutations on the spine (``files.*``, #665): the API is the one seam
    every file mutation passes through — operator doors and module bridges alike — so this
    router is where the core emits them. ``None`` disables emission (tests). *mounts*
    (#731) are operator-declared external roots addressed as ``mount:<name>/<sub-path>``;
    empty/``None`` means no mounts are declared. *scan_fuse* and *rescan* (#848) expose the
    mass de-index fuse: its state on ``GET /scan-status`` and the re-run door on
    ``POST /rescan`` (with ``force`` to purge anyway). Omitting either drops the pair of
    routes — a router built without them behaves exactly as before.
    """
    router = APIRouter(prefix="/platform/v1/files", tags=["files"])
    mounts = mounts or {}

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

    def _target(path: str) -> tuple[FileStore, str, Mount | None]:
        """Resolve *path* to ``(store, path-within-that-store, mount-or-None)`` (#731).

        A ``mount:<name>/<sub>`` path redirects to that mount's own store (*sub* is the
        remainder, ``""`` for the mount's own root); anything else addresses the tenant
        *store* unchanged, so every existing tenant-space call site is unaffected. 404s an
        unrecognised mount name before any filesystem call is made.
        """
        if not path.startswith(MOUNT_PREFIX):
            return store, path, None
        name, _, sub = path[len(MOUNT_PREFIX) :].partition("/")
        mount = mounts.get(name)
        if mount is None:
            raise HTTPException(status_code=404, detail=f"unknown mount: {name!r}")
        return mount.store, sub, mount

    def _require_writable(mount: Mount | None) -> None:
        """403 a mutation on a mount declared read-only (enforced here, not just hidden in UI)."""
        if mount is not None and mount.read_only:
            raise HTTPException(status_code=403, detail=f"mount {mount.name!r} is read-only")

    def _fs_movable(path: str, kind: str) -> bool:
        """Whether a tenant-space entry may be moved from the Files UI (#479).

        Operator-space *files* are movable, like object uploads; directories and anything
        under a module-owned top-level folder (*locked_prefixes*) stay read-only — moving a
        module's files behind its back would desync the module's own index.
        """
        if kind != "file":
            return False
        return path.split("/", 1)[0] not in locked_prefixes

    def _deletable(path: str) -> bool:
        """Whether a tenant-space entry may be deleted from the Files UI (#564).

        Broader than movability: *directories are deletable* (the delete seam is recursive —
        a folder takes its whole subtree), and it spans both stores (file-space files and
        object uploads). Only a module-owned top-level subtree (*locked_prefixes*) is off-limits
        — that lifecycle belongs to the owning page (#216/#340). Every listed entry has a
        non-empty path (the tenant root is never an item), and the ``delete`` door re-checks
        this rule server-side so a crafted request cannot bypass the hidden button.
        """
        return path.split("/", 1)[0] not in locked_prefixes

    def _movable_for(mount: Mount | None, path: str, kind: str) -> bool:
        """:func:`_fs_movable`, mount-aware (#731): a file in a RW mount is movable, like a
        tenant-space file; anything in a RO mount (including directories) is not."""
        if mount is not None:
            return not mount.read_only and kind == "file"
        return _fs_movable(path, kind)

    def _deletable_for(mount: Mount | None, path: str) -> bool:
        """:func:`_deletable`, mount-aware (#731): RW mirrors the tenant space, RO never is."""
        if mount is not None:
            return not mount.read_only
        return _deletable(path)

    def _full_path(mount: Mount | None, rel: str) -> str:
        """Reconstruct the caller-facing address for *rel* within *mount* (or the tenant space).

        A mount's own :class:`FileStore` knows nothing of the ``mount:<name>/`` addressing
        scheme (that's a router-level concept) — every response that echoes a path back (an
        entry, an event, an index row) must reprefix it, or a caller round-tripping that path
        (read-after-write, a follow-up move) would silently address the tenant space instead.
        """
        if mount is None:
            return rel
        return f"{MOUNT_PREFIX}{mount.name}/{rel}" if rel else f"{MOUNT_PREFIX}{mount.name}"

    def _reprefix(mount: Mount | None, entry: FileEntry) -> FileEntry:
        """Reprefix *entry*'s path with its mount address, if any (see :func:`_full_path`)."""
        if mount is None:
            return entry
        return entry.model_copy(update={"path": _full_path(mount, entry.path)})

    def _mount_for_indexed_path(path: str) -> Mount | None:
        """Which declared mount (if any) an already-indexed path (e.g. a search hit) is under.

        ``None`` both for a plain tenant-space path *and* for a stale row under a mount name
        that is no longer declared — the caller treats both the same way (tenant-space rules).
        """
        if not path.startswith(MOUNT_PREFIX):
            return None
        name, _, _sub = path[len(MOUNT_PREFIX) :].partition("/")
        return mounts.get(name)

    def _indexable(mount: Mount | None) -> bool:
        """Whether a mutation on *mount* should touch the index — opt-in per mount (#731)."""
        return mount is None or mount.indexed

    # ── Module-facing I/O (Phase 1) ──────────────────────────────────────────────

    @router.get("/list", response_model=FileListResponse)
    async def list_files(
        path: str = Query(default="", description="Directory to list (empty = tenant root)"),
        tenant_id: str | None = Query(default=None),
    ) -> FileListResponse:
        tenant = _tenant(tenant_id)
        target, sub, mount = _target(path)
        _safe(sub)
        entries = await target.list_dir(tenant=tenant, path=sub)
        return FileListResponse(entries=[_reprefix(mount, e) for e in entries])

    @router.get("/read", response_model=FileReadResponse)
    async def read_file(
        path: str = Query(..., description="File to read, relative to the tenant root"),
        tenant_id: str | None = Query(default=None),
    ) -> FileReadResponse:
        tenant = _tenant(tenant_id)
        target, sub, mount = _target(path)
        rel = _safe(sub)
        try:
            content = await target.read_text(tenant=tenant, path=sub)
        except FileNotFoundError:
            # Not in the file space — it may be an object-store entry (upload / agent object).
            # A mount has no object-store equivalent, so the fallback is tenant-space only.
            if objects is not None and mount is None:
                obj = await objects.read(tenant=tenant, path=rel)
                if obj is not None:
                    return FileReadResponse(path=obj.path, name=obj.name, content=obj.content)
            raise HTTPException(status_code=404, detail=_not_found_detail(path)) from None
        except PathEscapeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:  # the 256 KB text cap (traversal already handled by _safe)
            raise HTTPException(
                status_code=413, detail="file is too large to read as text"
            ) from exc
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="file is not UTF-8 text") from exc
        return FileReadResponse(
            path=_full_path(mount, rel), name=rel.rsplit("/", 1)[-1], content=content
        )

    @router.get("/stat", response_model=FileEntry)
    async def stat_file(
        path: str = Query(...),
        tenant_id: str | None = Query(default=None),
    ) -> FileEntry:
        tenant = _tenant(tenant_id)
        target, sub, mount = _target(path)
        _safe(sub)
        entry = await target.stat(tenant=tenant, path=sub)
        if entry is None:
            raise HTTPException(status_code=404, detail=_not_found_detail(path))
        return _reprefix(mount, entry)

    @router.put("/write", response_model=FileEntry)
    async def write_file(
        body: FileWriteBody,
        path: str = Query(...),
        tenant_id: str | None = Query(default=None),
    ) -> FileEntry:
        tenant = _tenant(tenant_id)
        target, sub, mount = _target(path)
        _require_writable(mount)
        _safe(sub)
        # Whether this write brings the path into existence — an overwrite emits nothing
        # (#665: there is deliberately no file_updated; content owners emit their own
        # *.updated events, so a mirror save must not double-signal here).
        created = events is not None and await target.stat(tenant=tenant, path=sub) is None
        try:
            entry = await target.write_text(tenant=tenant, path=sub, content=body.content)
        except PathEscapeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:  # e.g. writing to the store root itself
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if created and events is not None:
            await events.file_added(tenant, _full_path(mount, entry.path), size=entry.size)
        return _reprefix(mount, entry)

    @router.delete("", response_model=FileDeleteResponse)
    async def delete_file(
        path: str = Query(...),
        tenant_id: str | None = Query(default=None),
    ) -> FileDeleteResponse:
        tenant = _tenant(tenant_id)
        target, sub, mount = _target(path)
        _require_writable(mount)
        _safe(sub)
        try:
            deleted = await target.delete(tenant=tenant, path=sub)
        except PathEscapeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:  # deleting the store root is rejected
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if deleted and events is not None:
            await events.file_deleted(tenant, _full_path(mount, normalize_rel(sub)))
        return FileDeleteResponse(deleted=deleted)

    @router.post("/dir", response_model=FileEntry)
    async def make_dir(
        path: str = Query(...),
        tenant_id: str | None = Query(default=None),
    ) -> FileEntry:
        tenant = _tenant(tenant_id)
        target, sub, mount = _target(path)
        _require_writable(mount)
        _safe(sub)
        return _reprefix(mount, await target.ensure_dir(tenant=tenant, path=sub))

    @router.post("/move", response_model=FileEntry)
    async def move_file(
        body: FileMoveBody,
        tenant_id: str | None = Query(default=None),
    ) -> FileEntry:
        tenant = _tenant(tenant_id)
        src_store, src_sub, src_mount = _target(body.src)
        _dst_store, dst_sub, dst_mount = _target(body.dst)
        # A move stays within one store: the tenant space, or one named mount. Crossing
        # between them would mean copying real bytes between two independent filesystem
        # roots (or the tenant space and one) — a materially different operation from a
        # rename, and out of scope for v1 (#731); mounts stay isolated compartments, mirroring
        # how a module's own top-level subtree is already isolated via locked_prefixes below.
        src_name = src_mount.name if src_mount is not None else None
        dst_name = dst_mount.name if dst_mount is not None else None
        if src_name != dst_name:
            raise HTTPException(
                status_code=400,
                detail="cannot move between the tenant space and a mount, or across mounts",
            )
        _require_writable(dst_mount)
        src = _safe(src_sub)
        dst = _safe(dst_sub)
        _reject_pathological(dst)
        # Mirror the upload guard (#479) on the move/rename path (#554): refuse landing a file
        # *into* a module-owned top-level subtree from outside it. Tenant space only — a mount
        # is a separate namespace with its own RO/RW gate, already checked above. The src-top ≠
        # dst-top condition preserves a module's self-moves (it manages its own subtree, and the
        # module-facing files_move relies on that) — a foreign file landing behind a module's
        # back would desync its index, exactly what the upload 400 prevents. A rename whose typed
        # name smuggled in a leading path (making dst_top a locked module) is caught here too.
        if src_mount is None:
            src_top = src.split("/", 1)[0]
            dst_top = dst.split("/", 1)[0]
            if dst_top in locked_prefixes and src_top != dst_top:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"'{dst_top}' belongs to the {dst_top} module — move into your own folders"
                    ),
                )
        try:
            moved = await src_store.move(tenant=tenant, src=src, dst=dst)
        except FileNotFoundError:
            # Not a file-space entry — try the object store (a movable upload/agent object).
            # Mounts have no object-store equivalent.
            if objects is not None and src_mount is None:
                entry = await objects.move(tenant=tenant, src=src, dst=dst)
                if events is not None:
                    await events.file_moved(tenant, src, entry.path)
                return FileEntry(path=entry.path, name=entry.name, kind=entry.kind, size=entry.size)
            raise HTTPException(
                status_code=404, detail=f"source {_not_found_detail(src)}"
            ) from None
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="destination already exists") from exc
        except PathEscapeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:  # store root, or a move into the path itself
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        full_src = _full_path(src_mount, src)
        full_dst = _full_path(src_mount, moved.path)
        # Keep the index in step immediately so a moved file is searchable at once (the watcher
        # would also catch it, debounced). Best-effort: the move itself already succeeded.
        # Opt-in per mount (#731): skip entirely for a mount that never joined the index.
        if index is not None and _indexable(src_mount):
            with suppress(Exception):  # index freshness is best-effort; the move already stands
                await index.upsert_batch(
                    tenant=tenant,
                    entries=[
                        {
                            "path": full_dst,
                            "name": moved.name,
                            "size": moved.size,
                            "mtime": moved.mtime,
                            "kind": moved.kind,
                        }
                    ],
                )
        if events is not None:
            await events.file_moved(tenant, full_src, full_dst)
        return _reprefix(src_mount, moved)

    # ── Operator-facing Files UI (Phase 2) ───────────────────────────────────────

    async def _unused_path(*, store: FileStore, tenant: str, rel_dir: str, name: str) -> str:
        """The first collision-free path for *name* under *rel_dir* (photo.jpg → photo-2.jpg).

        Uploading must never silently replace an existing file; suffix the stem instead.
        Best-effort under concurrency (stat-then-write), which is fine for an operator UI.
        *store* is the resolved destination store — the tenant space or a mount (#731).
        """
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:  # "README", or a dotfile like ".env" — suffix the whole name
            stem, ext = name, ""
        for n in range(1, 1000):
            cand = name if n == 1 else (f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}")
            path = f"{rel_dir}/{cand}" if rel_dir else cand
            if await store.stat(tenant=tenant, path=path) is None:
                return path
        raise HTTPException(status_code=409, detail=f"too many files named like {name!r}")

    @router.post("/upload", response_model=FileEntry)
    async def upload_file(
        file: UploadFile,
        dir: str = Query(default="", description="Destination directory (empty = tenant root)"),
        tenant_id: str | None = Query(default=None),
    ) -> FileEntry:
        """Upload one file into the tenant file space or a declared mount — the Files page's
        upload door (#479; mounts: #731).

        Enforces the shared #175 caps — content-type allowlist (415) and byte cap (413) —
        then lands the bytes through the FileStore seam (local-FS ↔ S3, constraint #3) and
        indexes the entry immediately, so it is listed and searchable with no rescan. The
        multi-file UI sends one request per file, which is what per-file progress and
        failure states want. Module-owned destinations are refused (400), as is a read-only
        mount (403); a name collision gets a ``-2``/``-3``… suffix rather than overwriting.
        """
        tenant = _tenant(tenant_id)
        target, sub_dir, mount = _target(dir)
        _require_writable(mount)
        rel_dir = _safe(sub_dir)
        if mount is None:
            top = rel_dir.split("/", 1)[0]
            if top and top in locked_prefixes:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{top}' belongs to the {top} module — upload into your own folders",
                )
        kind = file.content_type or "application/octet-stream"
        if not content_type_allowed(kind, allowed_upload_types):
            raise HTTPException(status_code=415, detail=f"unsupported file type: {kind}")
        over_limit = f"file exceeds the {max_upload_bytes}-byte limit"
        # Starlette sets file.size from the parsed part — reject before reading the spool.
        if file.size is not None and file.size > max_upload_bytes:
            raise HTTPException(status_code=413, detail=over_limit)
        data = await file.read()
        if len(data) > max_upload_bytes:  # defense if size was unset or understated
            raise HTTPException(status_code=413, detail=over_limit)

        # The client sends a bare filename; a path-y one (odd browsers, curl) is reduced to
        # its basename — the destination directory is the `dir` param, never the filename.
        name = (file.filename or "file").replace("\\", "/").rsplit("/", 1)[-1].strip()
        if name in ("", ".", ".."):
            name = "file"
        _reject_pathological(name)
        rel_target = await _unused_path(store=target, tenant=tenant, rel_dir=rel_dir, name=name)
        try:
            entry = await target.write_bytes(
                tenant=tenant, path=rel_target, data=data, content_type=kind
            )
        except PathEscapeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        full = _full_path(mount, entry.path)
        # An upload always lands at an unused path (collisions get a suffix), so it is
        # always a genuinely-new file (#665).
        if events is not None:
            await events.file_added(tenant, full, size=entry.size)
        # Keep the index in step immediately so the upload is searchable at once (the
        # watcher would also catch it, debounced). Best-effort: the write already stands.
        # Opt-in per mount (#731): skip entirely for a mount that never joined the index.
        if index is not None and _indexable(mount):
            with suppress(Exception):
                await index.upsert_batch(
                    tenant=tenant,
                    entries=[
                        {
                            "path": full,
                            "name": entry.name,
                            "size": entry.size,
                            "mtime": entry.mtime,
                            "kind": entry.kind,
                        }
                    ],
                )
        return _reprefix(mount, entry)

    @router.delete("/entry", response_model=FileDeleteResponse)
    async def delete_entry(
        path: str = Query(..., description="Entry to delete (its whole subtree, if a folder)"),
        tenant_id: str | None = Query(default=None),
    ) -> FileDeleteResponse:
        """Delete an entry from the unified Files view — the Files page's delete door (#564;
        mounts: #731).

        The operator counterpart to the module-facing ``DELETE`` (they mirror ``upload`` vs
        ``write``): same FileStore seam, but this door adds the #479 ownership guard, an
        object-store fallback, and immediate de-indexing.

        * **Ownership guard.** A delete inside a module-owned top-level subtree
          (*locked_prefixes*) is refused (400) — that lifecycle belongs to the owning page
          (#216/#340). This is the same rule the UI hides behind ``deletable``, enforced here so
          a crafted request cannot bypass the missing button (the module ``DELETE`` stays
          unguarded precisely so modules *can* delete inside their own subtrees). A read-only
          mount is refused (403) the same way.
        * **Object fallback.** ``store.delete`` returns ``False`` when the path is not in the
          file space; then it may be a chat upload / agent object, so the delete falls through to
          the object store (symmetric to ``move``) — tenant space only, mounts have no object
          store equivalent.
        * **De-index.** A file-space delete removes the entry (and its subtree) from the core
          index at once, so it drops out of search/listing immediately; the #390 watcher is the
          backstop. Best-effort — the on-disk delete already stands. Skipped for a mount that
          never opted into indexing.

        Delete is recursive (a folder takes its whole subtree) and hard — no trash/undo in v1.
        The store root is refused by the seam (400). Returns ``{deleted}`` — ``False`` if
        nothing was at *path*.
        """
        tenant = _tenant(tenant_id)
        target, sub, mount = _target(path)
        _require_writable(mount)
        rel = _safe(sub)
        if mount is None:
            top = rel.split("/", 1)[0]
            if top and top in locked_prefixes:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{top}' belongs to the {top} module — delete it from its own page",
                )
        try:
            deleted = await target.delete(tenant=tenant, path=rel)
        except PathEscapeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:  # deleting the store root is rejected by the seam
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if deleted:
            full = _full_path(mount, rel)
            if index is not None and _indexable(mount):
                with suppress(Exception):  # de-index is best-effort; the delete already stands
                    await index.remove_subtree(tenant=tenant, path=full)
            if events is not None:
                await events.file_deleted(tenant, full)
            return FileDeleteResponse(deleted=True)
        # Not in the file space — it may be an object-store entry (chat upload / agent object).
        # Mounts have no object-store equivalent.
        if objects is not None and mount is None:
            object_deleted = await objects.delete(tenant=tenant, path=rel)
            if object_deleted and events is not None:
                await events.file_deleted(tenant, rel)
            return FileDeleteResponse(deleted=object_deleted)
        return FileDeleteResponse(deleted=False)

    @router.get("/page")
    async def files_page(
        path: str = Query(default="", description="Directory to browse (empty = root)"),
        q: str = Query(default="", description="Search query; if set, overrides path browsing"),
        tenant_id: str | None = Query(default=None),
    ) -> dict[str, object]:
        """Serve the Files browser page data (ADR-0018) over the unified file space.

        Returns a ``BrowserData``-shaped payload: ``{title, path, search_enabled, items,
        read_only}``. Merges the file-space tree (folders the user navigates, files they
        read/download) with the storage module's objects. Object entries (uploads /
        agent-written) and operator-space file-space files are ``movable``; directories and
        module-owned subtrees (*locked_prefixes*) are read-only in the UI (#479). Declared
        mounts (#731) render as additional top-level roots at ``path=""``; browsing into one
        lists its own tree (RW: same movable/deletable rules as the tenant space; RO: neither,
        and the page-level ``read_only`` flag tells the shell to hide Upload too — mutations are
        refused server-side regardless, this only spares the operator a doomed click).
        """
        tenant = _tenant(tenant_id)
        query = q.strip()
        items: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        read_only = False

        def _add(
            *, path: str, name: str, kind: str, size: int, movable: bool, deletable: bool
        ) -> None:
            # Dedupe the two sources by (kind, normalized path): a folder or file present in
            # both the file space and the object store collapses to one row (#560). The file
            # space is enumerated first, so it wins a collision — its ``movable`` reflects the
            # #479 ownership rule, where an object duplicate would wrongly force ``movable=True``.
            key = _dedupe_key(kind, path)
            if key in seen:
                return
            seen.add(key)
            items.append(
                _browser_item(
                    path=path,
                    name=name,
                    kind=kind,
                    size=size,
                    movable=movable,
                    deletable=deletable,
                )
            )

        current_mount: Mount | None = None
        if query:
            fs_hits = await index.search(tenant=tenant, query=query, limit=200) if index else []
            for e in fs_hits:
                hit_mount = _mount_for_indexed_path(e.path)
                _add(
                    path=e.path,
                    name=e.name,
                    kind=e.kind,
                    size=e.size,
                    movable=_movable_for(hit_mount, e.path, e.kind),
                    deletable=_deletable_for(hit_mount, e.path),
                )
            title = f"Files — {query}"
        else:
            target, sub, current_mount = _target(path)
            rel = _safe(sub)
            read_only = current_mount is not None and current_mount.read_only
            for fe in await target.list_dir(tenant=tenant, path=rel):
                full = _full_path(current_mount, fe.path)
                _add(
                    path=full,
                    name=fe.name,
                    kind=fe.kind,
                    size=fe.size,
                    movable=_movable_for(current_mount, fe.path, fe.kind),
                    deletable=_deletable_for(current_mount, fe.path),
                )
            # Declared mounts render as top-level roots, like a drive listing (#731) — only at
            # the tenant root; a mount's own children never nest another mount.
            if not rel and current_mount is None:
                for m in sorted(mounts.values(), key=lambda m: m.name):
                    _add(
                        path=f"{MOUNT_PREFIX}{m.name}",
                        name=m.name,
                        kind="dir",
                        size=0,
                        movable=False,
                        deletable=False,
                    )
            title = f"Files — {path}" if path else "Files"

        # Objects (chat uploads / agent objects) are tenant-scoped — never merged in while
        # browsing inside a mount subtree (`current_mount` stays None throughout a search, so
        # this still runs then, exactly as before).
        if objects is not None and current_mount is None:
            for oe in await objects.list(tenant=tenant, path=path, query=query):
                _add(
                    path=oe.path,
                    name=oe.name,
                    kind=oe.kind,
                    size=oe.size,
                    movable=True,
                    deletable=_deletable(oe.path),
                )

        # Dirs before files, then by name — the merged view reads like one tree.
        items.sort(key=lambda it: (it["icon"] != "folder", str(it["title"]).lower()))
        return {
            "title": title,
            "path": path,
            "search_enabled": True,
            "items": items,
            "read_only": read_only,
        }

    @router.get("/search", response_model=FileSearchResponse)
    async def search_files(
        q: str = Query(..., description="Name/path fragment to match"),
        limit: int = Query(default=50, ge=1, le=200),
        tenant_id: str | None = Query(default=None),
    ) -> FileSearchResponse:
        """Search the core file index by name/path fragment (backs ``files_search``).

        Spans every indexed mount alongside the tenant space with no extra code — an indexed
        mount's rows already carry their ``mount:<name>/`` address (#731), the same string the
        other file routes resolve, so a hit here is directly addressable.
        """
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

    if scan_fuse is not None:

        @router.get("/scan-status", response_model=ScanStatusResponse)
        async def scan_status(tenant_id: str | None = Query(default=None)) -> ScanStatusResponse:
            """Report the mass de-index fuse (#848): thresholds, and any refusing namespace.

            The operator-readable half of the fuse — the other half is the ``ERROR`` log line
            and the ``epicurus_core_file_scan_fuse_tripped`` gauge. A tripped namespace means
            the file space read empty (or nearly so) while its index rows are intact: the
            rows were **kept**, so the Files view still lists what is really there; check the
            mount before deciding the files are gone.

            Scoped to *tenant_id* like every other route here (constraint #1). ``ScanFuse``
            keys its state by ``(tenant, namespace)``, so an unscoped read would hand one
            tenant another's namespace names and row counts — inert while v1 is single-tenant,
            and exactly the leak that is invisible until it isn't.
            """
            tenant = _tenant(tenant_id)
            trips = [t for t in scan_fuse.trips() if t.tenant == tenant]
            return ScanStatusResponse(
                fuse_enabled=scan_fuse.enabled,
                max_delete_ratio=scan_fuse.max_delete_ratio,
                min_deletions=scan_fuse.min_deletions,
                tripped=bool(trips),
                namespaces=[
                    ScanFuseState(
                        tenant=t.tenant,
                        namespace=t.namespace,
                        indexed_rows=t.indexed_rows,
                        would_delete=t.would_delete,
                        seen_entries=t.seen_entries,
                        reason=t.reason,
                        at=t.at,
                    )
                    for t in trips
                ],
            )

    if rescan is not None:

        @router.post("/rescan", response_model=RescanResponse)
        async def rescan_file_space(
            namespace: str = Query(
                default="",
                description="Empty for the tenant tree, or an indexed mount's name",
            ),
            force: bool = Query(
                default=False,
                description="Purge stale index rows even if the mass de-index fuse refuses",
            ),
            tenant_id: str | None = Query(default=None),
        ) -> RescanResponse:
            """Re-run the file-space scan for one namespace (#848).

            The recovery door after a suspect mount: repair the mount, call this, and the
            index converges again. ``force=true`` says the emptiness is real and purges the
            stale rows the fuse is withholding — the only way to make the index follow a file
            space that genuinely lost most of its contents.

            *tenant_id* is threaded through to the scan and to the fuse read, so the answer
            describes the tenant that was asked about rather than whichever one tripped last
            (constraint #1). It defaults to the core's default tenant, which is the one the
            startup walk and the watchers this shares a lock with also run.
            """
            tenant = _tenant(tenant_id)
            try:
                entries = await rescan(namespace, force, tenant)
            except KeyError:
                raise HTTPException(
                    status_code=404, detail=f'"{namespace}" is not an indexed mount'
                ) from None
            label = namespace_for(f"{MOUNT_PREFIX}{namespace}/" if namespace else "")
            tripped = scan_fuse is not None and any(
                t.namespace == label and t.tenant == tenant for t in scan_fuse.trips()
            )
            return RescanResponse(
                namespace=namespace, entries=entries, forced=force, tripped=tripped
            )

    @router.get("/download")
    async def download_file(
        path: str = Query(..., description="File to download, relative to the tenant root"),
        tenant_id: str | None = Query(default=None),
    ) -> Response:
        """Stream a file from the unified file space — file-space first, then the object store."""
        tenant = _tenant(tenant_id)
        target, sub, mount = _target(path)
        rel = _safe(sub)
        try:
            entry = await target.stat(tenant=tenant, path=rel)
        except PathEscapeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if entry is not None and entry.kind == "file":
            data = await target.read_bytes(tenant=tenant, path=rel)
            media = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
            return Response(
                content=data,
                media_type=media,
                headers={"content-disposition": _disposition(entry.name)},
            )
        if objects is not None and mount is None:
            dl = await objects.download(tenant=tenant, path=rel)
            if dl is not None:
                return StreamingResponse(
                    dl.body,
                    media_type=dl.content_type,
                    headers={"content-disposition": _disposition(dl.name)},
                )
        raise HTTPException(status_code=404, detail="not found")

    return router
