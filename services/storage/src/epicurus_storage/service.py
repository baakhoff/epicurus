"""Storage module — MCP tools for the agent's file-access surface and object store.

File-tree tools (read-only, backed by the indexed HDD):
  storage_list    — list directory children
  storage_search  — search by name/path fragment
  storage_read    — read text file contents (256 KB size guard)
  storage_status  — indexed-entry counts + configured root
  storage_rescan  — re-walk the tree and refresh the index

Object-store tools (read/write, backed by MinIO):
  storage_object_put  — store a text object under a key
  storage_object_get  — retrieve a stored object by key

The /ingest HTTP endpoint (chat upload sink — ADR-0025) and the /download endpoint
(binary streaming, filesystem or object-backed) live on the FastAPI layer, as does
/pages/{page_id} (browser archetype data). ``ingest_object`` and
``load_object_download`` below are the testable logic those routes wrap.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from pydantic import BaseModel

from epicurus_core import EpicurusModule, PageSpec, UiAction, UiSection
from epicurus_storage.db import FileEntry, FileIndex
from epicurus_storage.object_store import ObjectStore
from epicurus_storage.scanner import scan
from epicurus_storage.settings import READ_MAX_BYTES

MODULE_NAME = "storage"

STORAGE_PAGE_ID = "files"

SCAN_COMPLETE_SUBJECT = "storage.scan.completed"

# Virtual top-level folder under which chat uploads are catalogued and stored.
UPLOADS_PREFIX = "uploads"


def _fmt_size(size: int) -> str:
    """Human-readable file size."""
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if size >= threshold:
            return f"{size / threshold:.1f} {unit}"
    return f"{size} B"


def _entry_to_item(entry: FileEntry, *, download_base: str) -> dict[str, Any]:
    """Convert a ``FileEntry`` to a ``BrowserItem``-shaped dict (ADR-0018)."""
    is_dir = entry.kind == "dir"
    subtitle = "directory" if is_dir else _fmt_size(entry.size)
    return {
        "id": entry.path,
        "title": entry.name,
        "subtitle": subtitle,
        "body": None,
        "icon": "folder" if is_dir else "file",
        "nav_path": entry.path if is_dir else None,
        "href": f"{download_base}?path={quote(entry.path)}" if not is_dir else None,
    }


def build_page_data(
    entries: list[FileEntry],
    *,
    path: str,
    query: str,
    download_base: str,
) -> dict[str, Any]:
    """Return ``BrowserData``-shaped dict for the Files left-nav page (ADR-0018).

    ``download_base`` is the URL prefix for the core download proxy, e.g.
    ``/platform/v1/modules/storage/download``.
    """
    if query:
        title = f"Files — {query}"
    elif path:
        title = f"Files — {path}"
    else:
        title = "Files"

    return {
        "title": title,
        "path": path,
        "search_enabled": True,
        "items": [_entry_to_item(e, download_base=download_base) for e in entries],
    }


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
    """Index rows that make a stored object at *key* browsable (mirrors a scanned file).

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
) -> dict[str, Any]:
    """Persist an uploaded file to the object store and catalogue it (ADR-0025).

    The bytes land in MinIO under ``uploads/<token>-<name>`` — ``token`` is the core
    attachment id, which guarantees uniqueness so two uploads of the same filename do
    not collide. A ``source="object"`` index row makes the upload browsable in the
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
    the same surfaces as a scanned file. Cataloguing is the crux: without it the object lives in
    MinIO but never appears in the Files page, which lists the index — not the bucket (#347).

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

    ``None`` means "not a catalogued object" — the caller falls back to the read-only
    filesystem. Only a ``source="object"`` **file** entry whose bytes are still present
    in MinIO resolves here.
    """
    entry = await index.get(tenant=tenant, path=path)
    if entry is None or entry.source != "object" or entry.kind != "file":
        return None
    stored = await objects.get_object(tenant=tenant, key=path)
    if stored is None:
        return None
    return ObjectDownload(name=entry.name, data=stored.data, content_type=stored.content_type)


# ── Inline text read (split-screen reader, #KB-refactor req 6) ────────────────


class TextContent(BaseModel):
    """A text file's contents for the right-panel reader: ``{path, name, content}``."""

    path: str
    name: str
    content: str


def load_text_file(root: Path, path: str) -> TextContent:
    """Read a UTF-8 text file under *root* for inline preview (the Files split-screen).

    Path-safety mirrors ``storage_read``. Raises ``HTTPException``: 400 (bad/traversal
    path or not a file), 404 (absent), 413 (larger than ``READ_MAX_BYTES``), 415 (binary
    / non-UTF-8). The read-only tree is the source — uploaded objects are handled by the
    route, which checks the object store first.
    """
    root_resolved = root.resolve()
    try:
        resolved = (root_resolved / path).resolve()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid path") from exc
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="path escapes storage root") from exc
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="not found")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="path is not a file")
    if resolved.stat().st_size > READ_MAX_BYTES:
        raise HTTPException(status_code=413, detail="file is too large to preview")
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="file is not UTF-8 text") from exc
    return TextContent(path=path, name=resolved.name, content=text)


def build_module(
    index: FileIndex,
    objects: ObjectStore,
    *,
    storage_root: str,
    tenant: str,
    hidden_prefixes: tuple[str, ...] = (),
) -> EpicurusModule:
    """Build the storage module and register its MCP tools.

    ``hidden_prefixes`` are top-level subtrees the **agent's** file tools never see — e.g.
    ``notes`` is private/attach-only, so the agent must not read note bodies through
    ``storage_read`` even though the operator browses them in the Files page (#KB-refactor).
    The operator-facing surfaces (the Files page, ``/read``, ``/download``) are unaffected.
    """
    hidden = tuple(p.strip("/") for p in hidden_prefixes if p.strip("/"))

    def _is_hidden(path: str) -> bool:
        clean = path.replace("\\", "/").strip("/")
        return any(clean == h or clean.startswith(h + "/") for h in hidden)

    module = EpicurusModule(
        MODULE_NAME,
        version="0.6.0",
        description=(
            "File-tree index (list, search, read) over the shared file space, plus "
            "app-managed object storage via MinIO, durable chat-upload ingest, and "
            "inline text preview for the Files split-screen reader. Private subtrees "
            "(e.g. notes) are hidden from the agent's file tools."
        ),
        ui=UiSection(
            icon="folder",
            summary="Browse, search, and read the indexed file tree; manage platform objects.",
            config_schema={
                "type": "object",
                "properties": {
                    "storage_root": {
                        "type": "string",
                        "title": "Storage root",
                        "description": "Absolute path to the directory tree being indexed.",
                    },
                    "indexed_count": {
                        "type": "string",
                        "title": "Indexed entries",
                        "description": "Run 'Show status' to see current counts.",
                        "readOnly": True,
                    },
                },
            },
            actions=[
                UiAction(
                    tool="storage_status",
                    label="Show status",
                    description="Display the storage root and current indexed-entry counts.",
                ),
                UiAction(
                    tool="storage_rescan",
                    label="Re-scan now",
                    description="Re-index the file tree to pick up recent changes.",
                ),
            ],
        ),
        pages=[
            PageSpec(
                id=STORAGE_PAGE_ID,
                title="Files",
                archetype="browser",
                icon="folder",
                nav_order=10,
            )
        ],
    )

    module.emits(SCAN_COMPLETE_SUBJECT, "published after each full directory scan")

    # ── File-tree tools ─────────────────────────────────────────────────────

    @module.tool()
    async def storage_list(path: str = "") -> list[FileEntry]:
        """List the direct children of *path* in the indexed file tree.

        Pass an empty string (the default) to list the root.
        Returns directories before files, both sorted by name.
        """
        if _is_hidden(path):
            return []
        entries = await index.browse(tenant=tenant, path=path)
        return [e for e in entries if not _is_hidden(e.path)]

    @module.tool()
    async def storage_search(query: str, limit: int = 50) -> list[FileEntry]:
        """Search indexed files and directories by name or path fragment.

        Case-insensitive; returns up to *limit* results (max 200).
        """
        if not query.strip():
            return []
        results = await index.search(tenant=tenant, query=query, limit=max(1, min(limit, 200)))
        return [e for e in results if not _is_hidden(e.path)]

    @module.tool()
    async def storage_read(path: str) -> str:
        """Read a text file from the indexed tree and return its contents.

        *path* is relative to the configured storage root.
        Files larger than 256 KB are rejected — use the /download endpoint instead.
        Binary (non-UTF-8) files are also rejected with an explanatory message.
        """
        # Private subtrees (e.g. notes) are never readable by the agent (#KB-refactor).
        if _is_hidden(path):
            return "Error: not available"
        # An agent-written object (source="object") lives in MinIO, not on disk — read it back
        # from the store so a file the agent just saved is readable through the same tool that
        # now lists it (#347). Everything else is a file on the read-only tree, handled below.
        entry = await index.get(tenant=tenant, path=path)
        if entry is not None and entry.source == "object" and entry.kind == "file":
            stored = await objects.get_object(tenant=tenant, key=entry.path)
            if stored is None:
                return "Error: file not found"
            if len(stored.data) > READ_MAX_BYTES:
                return (
                    f"Error: file is too large ({len(stored.data):,} bytes); "
                    f"maximum is {READ_MAX_BYTES:,} bytes — use /download instead"
                )
            try:
                return stored.data.decode("utf-8")
            except UnicodeDecodeError:
                return "Error: file is not valid UTF-8 (binary file)"
        root = Path(storage_root).resolve()
        try:
            resolved = (root / path).resolve()
        except Exception:
            return "Error: invalid path"

        try:
            resolved.relative_to(root)
        except ValueError:
            return "Error: path escapes storage root"

        if not resolved.exists():
            return "Error: file not found"
        if not resolved.is_file():
            return "Error: path is not a file"

        size = resolved.stat().st_size
        if size > READ_MAX_BYTES:
            return (
                f"Error: file is too large ({size:,} bytes); "
                f"maximum is {READ_MAX_BYTES:,} bytes — use /download instead"
            )

        try:
            return resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "Error: file is not valid UTF-8 (binary file)"

    @module.tool()
    async def storage_status() -> dict[str, object]:
        """Return storage-module status: configured root and indexed-entry counts."""
        counts = await index.count(tenant=tenant)
        return {"root": storage_root, **counts}

    @module.tool()
    async def storage_rescan() -> dict[str, int]:
        """Re-scan the configured root directory and update the index.

        Returns ``{"total": N}`` where N is the number of entries visited.
        """
        total = await scan(Path(storage_root), index, tenant=tenant)
        return {"total": total}

    # ── Object-store tools ───────────────────────────────────────────────────

    @module.tool()
    async def storage_object_put(key: str, content: str) -> dict[str, str]:
        """Store *content* (UTF-8 text) as an object under *key*, visible in the Files page.

        Objects are scoped to the current tenant's bucket and are writable — unlike the
        read-only file tree, these are platform-managed. The object is catalogued on write, so
        it appears in the Files page and is searchable, readable, and downloadable like any
        other file; a nested key (e.g. ``reports/q2.md``) creates the enclosing folders. The
        returned ``key`` is the normalised path actually used. Returns
        ``{"status": "ok", "key": key}``.
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
