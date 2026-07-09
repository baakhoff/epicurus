"""Storage module — the agent's file-access tools and the app-managed object store.

After the file-space migration (ADR-0063) the **core** owns the unified file space and serves
the Files browser UI; this module is a *consumer* of it plus the owner of the object store:

Agent file tools (read the core-owned file space via the platform API, no ``/data`` mount):
  storage_list    — list directory children (file space + objects)
  storage_search  — search by name/path fragment (file space + objects)
  storage_read    — read a text file (object store first, then the file space)
  storage_status  — object-store entry counts

Object-store tools (read/write, backed by MinIO):
  storage_object_put  — store a text object under a key
  storage_object_get  — retrieve a stored object by key

The /ingest HTTP endpoint (chat upload sink — ADR-0025) and the object surface the core's
Files view proxies (``/objects``, ``/objects/read``, ``/download``, ``/objects/move``) live on
the FastAPI layer. ``ingest_object``, ``put_object``, ``load_object_download`` and ``move_item``
below are the testable logic those routes wrap.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from pydantic import BaseModel

from epicurus_core import EpicurusModule, PlatformClient, UiAction, UiSection, get_logger
from epicurus_storage.db import FileIndex
from epicurus_storage.object_store import ObjectStore
from epicurus_storage.settings import READ_MAX_BYTES

MODULE_NAME = "storage"

log = get_logger(MODULE_NAME)

# Virtual top-level folder under which chat uploads are catalogued and stored.
UPLOADS_PREFIX = "uploads"


class FileNode(BaseModel):
    """A file-space node as the agent file tools return it (file-space or object)."""

    path: str
    name: str
    kind: str
    size: int


# ── Chat upload sink (ADR-0025) ──────────────────────────────────────────────


def _safe_name(filename: str) -> str:
    """Reduce *filename* to a safe basename: no path separators, never empty."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return base or "file"


def _index_row(path: str, name: str, size: int, kind: str) -> dict[str, object]:
    """An ``upsert_batch`` entry; objects carry no filesystem mtime, hence 0.0."""
    return {"path": path, "name": name, "size": size, "mtime": 0.0, "kind": kind}


def _normalize_key(key: str) -> str:
    """Normalise an object key to a POSIX-relative path safe for the index and the browser.

    Collapses back-slashes and drops empty / ``.`` / ``..`` segments (no traversal), so the
    one string addresses the object in MinIO, its row in the index, and its node in the Files
    tree. Returns ``""`` only for an all-junk key; callers supply a fallback.
    """
    parts = [seg for seg in key.replace("\\", "/").split("/") if seg not in ("", ".", "..")]
    return "/".join(parts)


def _object_index_rows(key: str, *, name: str, size: int) -> list[dict[str, object]]:
    """Index rows that make a stored object at *key* browsable (mirrors a file-space file).

    Every parent segment of *key* becomes a ``dir`` row so the Files tree can drill into it,
    and the leaf is a ``file`` row carrying the display *name* and byte *size*. ``source`` is
    stamped ``"object"`` by the :meth:`FileIndex.upsert_batch` caller. *key* must already be
    normalised (see :func:`_normalize_key`).
    """
    parts = key.split("/")
    rows = [_index_row("/".join(parts[: i + 1]), parts[i], 0, "dir") for i in range(len(parts) - 1)]
    rows.append(_index_row(key, name, size, "file"))
    return rows


async def ingest_object(
    *,
    index: FileIndex,
    objects: ObjectStore,
    tenant: str,
    att_id: str,
    filename: str,
    content_type: str,
    data: bytes,
) -> dict[str, object]:
    """Persist an uploaded file to the object store and catalogue it (ADR-0025).

    The bytes land in MinIO under ``uploads/<token>-<name>`` — ``token`` is the core
    attachment id, which guarantees uniqueness so two uploads of the same filename do
    not collide. A ``source="object"`` index row makes the upload browsable in the core
    Files page (and findable via search), and an ``uploads`` directory row gives it a
    home at the tree root. Returns ``{key, name, size}``.
    """
    name = _safe_name(filename)
    token = att_id.strip() or uuid.uuid4().hex
    key = f"{UPLOADS_PREFIX}/{token}-{name}"
    await objects.put_bytes(
        tenant=tenant,
        key=key,
        data=data,
        content_type=content_type or "application/octet-stream",
    )
    # The "uploads" folder row gives uploads a home at the tree root; the file row is
    # the upload itself (display name kept separate from the unique storage key).
    await index.upsert_batch(
        tenant=tenant,
        source="object",
        entries=_object_index_rows(key, name=name, size=len(data)),
    )
    return {"key": key, "name": name, "size": len(data)}


async def put_object(
    *, index: FileIndex, objects: ObjectStore, tenant: str, key: str, content: str
) -> dict[str, str]:
    """Store a UTF-8 text object under *key* and catalogue it so it shows in the Files UI.

    The agent's ``storage_object_put`` tool writes here. Like :func:`ingest_object`, the bytes
    land in the tenant object bucket **and** a ``source="object"`` index row (plus any ancestor
    directory rows) makes the object browsable, searchable, readable, and downloadable through
    the same surfaces as a file-space file. Cataloguing is the crux: without it the object lives
    in MinIO but never appears in the Files page, which lists the index — not the bucket (#347).

    The key is normalised (see :func:`_normalize_key`) and the same normalised string is used
    for the bucket key, the index path, and the returned ``key`` so a later read/download
    resolves. Returns ``{"status": "ok", "key": <normalised-key>}``.
    """
    clean = _normalize_key(key) or "file"
    size = len(content.encode("utf-8"))
    await objects.put(tenant=tenant, key=clean, content=content)
    await index.upsert_batch(
        tenant=tenant,
        source="object",
        entries=_object_index_rows(clean, name=clean.rsplit("/", 1)[-1], size=size),
    )
    return {"status": "ok", "key": clean}


@dataclass(frozen=True)
class ObjectDownload:
    """A resolved object-store download: the bytes plus how to present them."""

    name: str
    data: bytes
    content_type: str


async def load_object_download(
    *, index: FileIndex, objects: ObjectStore, tenant: str, path: str
) -> ObjectDownload | None:
    """Resolve *path* to an object-store download, or ``None`` if it is not one.

    ``None`` means "not a catalogued object" — the caller falls back to the core file space.
    Only a ``source="object"`` **file** entry whose bytes are still present in MinIO resolves
    here.
    """
    entry = await index.get(tenant=tenant, path=path)
    if entry is None or entry.source != "object" or entry.kind != "file":
        return None
    stored = await objects.get_object(tenant=tenant, key=path)
    if stored is None:
        return None
    return ObjectDownload(name=entry.name, data=stored.data, content_type=stored.content_type)


# ── Move / rename (object store, #381 / #391) ────────────────────────────────


async def move_item(
    *,
    index: FileIndex,
    objects: ObjectStore,
    tenant: str,
    from_path: str,
    to_path: str,
) -> dict[str, str]:
    """Move or rename an object-store entry; the ``/objects/move`` route wraps this.

    Only object-store entries (chat uploads and agent-written objects) are movable — the
    file-space tree is owned by the core (and the modules whose subtrees it holds). Renaming is
    the same-parent case of moving. Raises ``HTTPException``: 400 (root, or a move into the path
    itself), 404 (source absent / not an object), 409 (destination occupied). Returns
    ``{"path": <new-path>}``.

    Two stores must stay consistent — MinIO holds the bytes, the index is what the Files page and
    downloads read. So every object is copied to its new key first, the index subtree is then
    re-pathed in one transaction, and only then are the originals dropped. A crash between steps
    leaves harmless orphan copies in MinIO, never an index row pointing at missing bytes.
    """
    src = _normalize_key(from_path)
    dst = _normalize_key(to_path)
    if not src or not dst:
        raise HTTPException(status_code=400, detail="cannot move the file-space root")
    if src == dst:
        return {"path": dst}
    if dst.startswith(src + "/"):
        raise HTTPException(status_code=400, detail="cannot move a path into itself")

    if await index.get(tenant=tenant, path=src) is None:
        raise HTTPException(status_code=404, detail=f"no such file: {src}")

    subtree = await index.subtree(tenant=tenant, path=src)
    if any(e.source != "object" for e in subtree):
        raise HTTPException(
            status_code=400,
            detail="this entry is read-only — only uploaded or app-written files can be moved",
        )

    # The destination must be entirely free: nothing at dst and nothing already under dst/.
    if await index.get(tenant=tenant, path=dst) is not None or await index.subtree(
        tenant=tenant, path=dst
    ):
        raise HTTPException(status_code=409, detail=f"destination already exists: {dst}")

    # Make the destination's parent folders navigable: a move into a brand-new folder must
    # create its dir rows (mirroring how an upload creates its ancestors), or the moved entry
    # would be unreachable from the parent listing.
    parents = dst.split("/")[:-1]
    for i in range(len(parents)):
        ancestor = "/".join(parents[: i + 1])
        if await index.get(tenant=tenant, path=ancestor) is None:
            await index.upsert_batch(
                tenant=tenant,
                source="object",
                entries=[_index_row(ancestor, parents[i], 0, "dir")],
            )

    file_moves = [(e.path, dst + e.path[len(src) :]) for e in subtree if e.kind == "file"]
    for old_key, new_key in file_moves:
        await objects.copy(tenant=tenant, src_key=old_key, dst_key=new_key)
    await index.repath(tenant=tenant, src=src, dst=dst)
    for old_key, _ in file_moves:
        await objects.delete(tenant=tenant, key=old_key)
    return {"path": dst}


# ── Delete (object store, #564) ──────────────────────────────────────────────


async def delete_item(
    *,
    index: FileIndex,
    objects: ObjectStore,
    tenant: str,
    path: str,
) -> dict[str, bool]:
    """Delete a writable object-store entry (and its subtree); the ``DELETE /objects`` route
    wraps this. The core proxies here as the fallback for a Files-page delete whose path is not
    in the core file space (a chat upload or agent-written object, #564).

    Mirrors :func:`move_item`: only object-store entries are deletable — a read-only ``fs`` row
    (400) belongs to whatever owns it, and the file-space root (empty path) is refused (400).
    A missing entry is a clean ``{"deleted": False}`` (idempotent, like the FileStore seam), so
    the core can distinguish "nothing here" from a real failure.

    Byte-store first, index last, mirroring how the download resolves an object: the MinIO objects
    are dropped, then the index subtree in one commit. A crash between steps leaves harmless orphan
    index rows pointing at absent bytes — a rescan never revisits ``object`` rows, so the operator
    simply deletes again; it never leaves bytes stranded with a live, downloadable row.
    """
    key = _normalize_key(path)
    if not key:
        raise HTTPException(status_code=400, detail="cannot delete the file-space root")
    subtree = await index.subtree(tenant=tenant, path=key)
    if not subtree:
        return {"deleted": False}
    if any(e.source != "object" for e in subtree):
        raise HTTPException(
            status_code=400,
            detail="this entry is read-only — only uploaded or app-written files can be deleted",
        )
    for entry in subtree:
        if entry.kind == "file":
            await objects.delete(tenant=tenant, key=entry.path)
    await index.delete_subtree(tenant=tenant, path=key)
    return {"deleted": True}


def build_module(
    index: FileIndex,
    objects: ObjectStore,
    *,
    platform: PlatformClient,
    tenant: str,
    hidden_prefixes: tuple[str, ...] = (),
) -> EpicurusModule:
    """Build the storage module and register its MCP tools.

    The agent file tools read the **core-owned file space** through *platform*
    (``PlatformClient.files_*``) — the module no longer mounts ``/data`` (ADR-0063) — and merge
    in the module's own object store. ``hidden_prefixes`` are top-level subtrees the **agent's**
    file tools never see (e.g. ``notes`` is private/attach-only); the operator still browses them
    in the core Files page, which is unaffected by this gate.
    """
    hidden = tuple(p.strip("/") for p in hidden_prefixes if p.strip("/"))

    def _is_hidden(path: str) -> bool:
        clean = path.replace("\\", "/").strip("/")
        return any(clean == h or clean.startswith(h + "/") for h in hidden)

    module = EpicurusModule(
        MODULE_NAME,
        version="0.9.0",
        description=(
            "Agent file tools over the core-owned file space (list, search, read), plus "
            "app-managed object storage via MinIO and durable chat-upload ingest. The Files "
            "browser UI is served by the core (ADR-0063); private subtrees (e.g. notes) are "
            "hidden from the agent's file tools."
        ),
        ui=UiSection(
            icon="folder",
            summary="Agent file tools over the file space; manage platform objects.",
            config_schema={
                "type": "object",
                "properties": {
                    "indexed_count": {
                        "type": "string",
                        "title": "Stored objects",
                        "description": "Run 'Show status' to see the object-store entry counts.",
                        "readOnly": True,
                    },
                },
            },
            actions=[
                UiAction(
                    tool="storage_status",
                    label="Show status",
                    description="Display the current object-store entry counts.",
                ),
            ],
        ),
    )

    # ── File-tree tools (over the core-owned file space + the object store) ───

    async def _fs_nodes(path: str) -> list[FileNode]:
        """File-space children of *path*, via the platform API; empty if the core is down."""
        try:
            return [
                FileNode(path=e.path, name=e.name, kind=e.kind, size=e.size)
                for e in await platform.files_list(path)
                if not _is_hidden(e.path)
            ]
        except httpx.HTTPError as exc:
            log.warning("file-space list failed; returning objects only", error=str(exc))
            return []

    @module.tool()
    async def storage_list(path: str = "") -> list[FileNode]:
        """List the direct children of *path* in the file space (file-space + objects).

        Pass an empty string (the default) to list the root. Returns directories before files,
        both sorted by name.
        """
        if _is_hidden(path):
            return []
        nodes = await _fs_nodes(path)
        nodes.extend(
            FileNode(path=o.path, name=o.name, kind=o.kind, size=o.size)
            for o in await index.browse(tenant=tenant, path=path)
            if not _is_hidden(o.path)
        )
        nodes.sort(key=lambda n: (n.kind != "dir", n.name.lower()))
        return nodes

    @module.tool()
    async def storage_search(query: str, limit: int = 50) -> list[FileNode]:
        """Search the file space (file-space + objects) by name or path fragment.

        Case-insensitive; returns up to *limit* results (max 200).
        """
        if not query.strip():
            return []
        capped = max(1, min(limit, 200))
        nodes: list[FileNode] = []
        try:
            nodes.extend(
                FileNode(path=e.path, name=e.name, kind=e.kind, size=e.size)
                for e in await platform.files_search(query, limit=capped)
                if not _is_hidden(e.path)
            )
        except httpx.HTTPError as exc:
            log.warning("file-space search failed; returning objects only", error=str(exc))
        nodes.extend(
            FileNode(path=o.path, name=o.name, kind=o.kind, size=o.size)
            for o in await index.search(tenant=tenant, query=query, limit=capped)
            if not _is_hidden(o.path)
        )
        nodes.sort(key=lambda n: (n.kind != "dir", n.name.lower()))
        return nodes[:capped]

    @module.tool()
    async def storage_read(path: str) -> str:
        """Read a text file and return its contents (object store first, then file space).

        Files larger than 256 KB are rejected — use the Files download instead. Binary
        (non-UTF-8) files are also rejected with an explanatory message.
        """
        # Private subtrees (e.g. notes) are never readable by the agent (#KB-refactor).
        if _is_hidden(path):
            return "Error: not available"
        # An agent-written object (source="object") lives in MinIO — read it back from the store
        # so a file the agent just saved is readable through the same tool that lists it (#347).
        obj = await load_object_download(index=index, objects=objects, tenant=tenant, path=path)
        if obj is not None:
            if len(obj.data) > READ_MAX_BYTES:
                return (
                    f"Error: file is too large ({len(obj.data):,} bytes); "
                    f"maximum is {READ_MAX_BYTES:,} bytes — use the Files download instead"
                )
            try:
                return obj.data.decode("utf-8")
            except UnicodeDecodeError:
                return "Error: file is not valid UTF-8 (binary file)"
        # Otherwise it is a file-space file — read it through the core file API.
        try:
            return await platform.files_read(path)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 404:
                return "Error: file not found"
            if code == 413:
                return (
                    f"Error: file is too large; maximum is {READ_MAX_BYTES:,} bytes — "
                    "use the Files download instead"
                )
            if code == 415:
                return "Error: file is not valid UTF-8 (binary file)"
            return f"Error: read failed (HTTP {code})"
        except httpx.HTTPError:
            return "Error: file space unavailable"

    @module.tool()
    async def storage_status() -> dict[str, object]:
        """Return storage-module status: object-store entry counts."""
        counts = await index.count(tenant=tenant)
        return {"object_files": counts["files"], "object_dirs": counts["dirs"]}

    # ── Object-store tools ───────────────────────────────────────────────────

    @module.tool()
    async def storage_object_put(key: str, content: str) -> dict[str, str]:
        """Store *content* (UTF-8 text) as an object under *key*, visible in the Files page.

        Objects are scoped to the current tenant's bucket and are writable — unlike the file
        space, these are platform-managed. The object is catalogued on write, so it appears in
        the Files page and is searchable, readable, and downloadable like any other file; a
        nested key (e.g. ``reports/q2.md``) creates the enclosing folders. The returned ``key``
        is the normalised path actually used. Returns ``{"status": "ok", "key": key}``.
        """
        return await put_object(
            index=index, objects=objects, tenant=tenant, key=key, content=content
        )

    @module.tool()
    async def storage_object_get(key: str) -> dict[str, str | None]:
        """Retrieve the text content of the object stored under *key*.

        Returns ``{"key": key, "content": "..."}`` or
        ``{"key": key, "content": null}`` if the key does not exist.
        """
        content = await objects.get(tenant=tenant, key=_normalize_key(key) or key)
        return {"key": key, "content": content}

    return module
