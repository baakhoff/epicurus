"""Storage module — MCP tools for browse, search, and rescan.

The download endpoint lives on the FastAPI layer (binary streaming doesn't fit
the text-based MCP tool contract).
"""

from __future__ import annotations

from pathlib import Path

from epicurus_core import EpicurusModule, UiAction, UiSection
from epicurus_storage.db import FileEntry, FileIndex
from epicurus_storage.scanner import scan

MODULE_NAME = "storage"

SCAN_COMPLETE_SUBJECT = "storage.scan.completed"


def build_module(index: FileIndex, *, storage_root: str, tenant: str) -> EpicurusModule:
    """Build the storage module and register its tools."""
    module = EpicurusModule(
        MODULE_NAME,
        version="0.1.0",
        description="Read-only file-tree index: browse, search, and download files.",
        ui=UiSection(
            summary="Browse and search the indexed file tree on disk.",
            config_schema={
                "type": "object",
                "properties": {
                    "storage_root": {
                        "type": "string",
                        "title": "Storage root",
                        "description": "Absolute path to the directory tree to index.",
                    }
                },
            },
            actions=[
                UiAction(
                    tool="storage_rescan",
                    label="Re-scan now",
                    description="Re-index the file tree to pick up recent changes.",
                )
            ],
        ),
    )

    module.emits(SCAN_COMPLETE_SUBJECT, "published after each full directory scan")

    @module.tool()
    async def storage_browse(path: str = "") -> list[FileEntry]:
        """List the direct children of *path* in the indexed file tree.

        Use an empty string (the default) to list the root.
        Returns directories before files, both sorted by name.
        """
        return await index.browse(tenant=tenant, path=path)

    @module.tool()
    async def storage_search(query: str, limit: int = 50) -> list[FileEntry]:
        """Search indexed files and directories by name or path fragment.

        Case-insensitive; returns up to *limit* results.
        """
        if not query.strip():
            return []
        return await index.search(tenant=tenant, query=query, limit=max(1, min(limit, 200)))

    @module.tool()
    async def storage_rescan() -> dict[str, int]:
        """Re-scan the configured root directory and update the index.

        Returns ``{"total": N}`` where N is the number of entries visited.
        """
        total = await scan(Path(storage_root), index, tenant=tenant)
        return {"total": total}

    return module
