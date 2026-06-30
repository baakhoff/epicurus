"""Walk the core file space and keep the :class:`FileIndex` current (ADR-0063).

The scan reads the :class:`~epicurus_core.files.FileStore` and writes only the DB index —
it never mutates the file space. It recurses ``list_dir`` rather than touching the
filesystem directly, so the same walk indexes a local-FS tree and an S3 bucket behind the
same contract (constraint #3). Entries seen are upserted; entries gone from the store are
purged, so a deleted file leaves search on the next pass.
"""

from __future__ import annotations

from epicurus_core import get_logger
from epicurus_core.files import FileStore
from epicurus_core_app.file_index import FileIndex

log = get_logger("core.file_scan")

_BATCH = 500  # flush to DB every N entries


async def scan(store: FileStore, index: FileIndex, *, tenant: str) -> int:
    """Walk the *tenant* file space via *store* and sync the DB *index*.

    Returns the total number of entries visited. The tenant root itself is not indexed
    (it has no name worth storing) — only its descendants, mirroring how a directory tree
    is browsed from the root down.
    """
    log.info("file scan started", tenant=tenant)
    visited: set[str] = set()
    batch: list[dict[str, object]] = []
    # DFS over directories via the backend-agnostic listing; "" is the tenant root.
    stack: list[str] = [""]
    while stack:
        directory = stack.pop()
        children = await store.list_dir(tenant=tenant, path=directory)
        for entry in children:
            visited.add(entry.path)
            batch.append(
                {
                    "path": entry.path,
                    "name": entry.name,
                    "size": entry.size,
                    "mtime": entry.mtime,
                    "kind": entry.kind,
                }
            )
            if entry.kind == "dir":
                stack.append(entry.path)
            if len(batch) >= _BATCH:
                await index.upsert_batch(tenant=tenant, entries=batch)
                batch.clear()

    if batch:
        await index.upsert_batch(tenant=tenant, entries=batch)

    deleted = await index.purge_stale(tenant=tenant, seen_paths=visited)
    total = len(visited)
    log.info("file scan complete", total=total, deleted=deleted, tenant=tenant)
    return total
