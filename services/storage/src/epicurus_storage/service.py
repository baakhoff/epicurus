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

The /download HTTP endpoint (binary streaming) lives on the FastAPI layer.
The /pages/{page_id} HTTP endpoint (browser archetype data) lives on the FastAPI layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from epicurus_core import EpicurusModule, PageSpec, UiAction, UiSection
from epicurus_storage.db import FileEntry, FileIndex
from epicurus_storage.object_store import ObjectStore
from epicurus_storage.scanner import scan
from epicurus_storage.settings import READ_MAX_BYTES

MODULE_NAME = "storage"

STORAGE_PAGE_ID = "files"

SCAN_COMPLETE_SUBJECT = "storage.scan.completed"


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


def build_module(
    index: FileIndex,
    objects: ObjectStore,
    *,
    storage_root: str,
    tenant: str,
) -> EpicurusModule:
    """Build the storage module and register its MCP tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.2.0",
        description=(
            "File-tree index (list, search, read) over the operator's HDD, "
            "plus app-managed object storage via MinIO."
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
        return await index.browse(tenant=tenant, path=path)

    @module.tool()
    async def storage_search(query: str, limit: int = 50) -> list[FileEntry]:
        """Search indexed files and directories by name or path fragment.

        Case-insensitive; returns up to *limit* results (max 200).
        """
        if not query.strip():
            return []
        return await index.search(tenant=tenant, query=query, limit=max(1, min(limit, 200)))

    @module.tool()
    async def storage_read(path: str) -> str:
        """Read a text file from the indexed tree and return its contents.

        *path* is relative to the configured storage root.
        Files larger than 256 KB are rejected — use the /download endpoint instead.
        Binary (non-UTF-8) files are also rejected with an explanatory message.
        """
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
        """Store *content* (UTF-8 text) as an object under *key*.

        Objects are scoped to the current tenant's bucket and are writable —
        unlike the read-only file tree, these are platform-managed.
        Returns ``{"status": "ok", "key": key}``.
        """
        await objects.put(tenant=tenant, key=key, content=content)
        return {"status": "ok", "key": key}

    @module.tool()
    async def storage_object_get(key: str) -> dict[str, str | None]:
        """Retrieve the text content of the object stored under *key*.

        Returns ``{"key": key, "content": "..."}`` or
        ``{"key": key, "content": null}`` if the key does not exist.
        """
        content = await objects.get(tenant=tenant, key=key)
        return {"key": key, "content": content}

    return module
