"""Directory scanner — walks *storage_root* and keeps the Postgres index current.

The scan is read-only with respect to the filesystem; it only writes to the DB.
Entries added since the last scan are inserted; removed entries are deleted.
"""

from __future__ import annotations

import os
from pathlib import Path

from epicurus_core import get_logger
from epicurus_storage.db import FileIndex

log = get_logger("storage.scanner")

_BATCH = 500  # flush to DB every N entries


async def scan(root: Path, index: FileIndex, *, tenant: str) -> int:
    """Walk *root* and sync the DB index for *tenant*.

    Returns the total number of filesystem entries visited.
    """
    log.info("scan started", root=str(root), tenant=tenant)
    visited: set[str] = set()
    batch: list[dict[str, object]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dir_abs = Path(dirpath)
        dir_rel = dir_abs.relative_to(root).as_posix()
        if dir_rel == ".":
            dir_rel = ""

        # Index the directory itself (skip the root — it has no name worth storing).
        if dir_rel:
            try:
                st = dir_abs.stat()
            except OSError:
                dirnames.clear()
                continue
            batch.append(
                {
                    "path": dir_rel,
                    "name": dir_abs.name,
                    "size": 0,
                    "mtime": st.st_mtime,
                    "kind": "dir",
                }
            )
            visited.add(dir_rel)

        for fname in filenames:
            fpath = dir_abs / fname
            try:
                st = fpath.stat()
            except OSError:
                continue
            rel = fpath.relative_to(root).as_posix()
            batch.append(
                {
                    "path": rel,
                    "name": fname,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "kind": "file",
                }
            )
            visited.add(rel)

            if len(batch) >= _BATCH:
                await index.upsert_batch(tenant=tenant, entries=batch)
                batch.clear()

    if batch:
        await index.upsert_batch(tenant=tenant, entries=batch)

    deleted = await index.purge_stale(tenant=tenant, seen_paths=visited)
    total = len(visited)
    log.info("scan complete", total=total, deleted=deleted, tenant=tenant)
    return total
