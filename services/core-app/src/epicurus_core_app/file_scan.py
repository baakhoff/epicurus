"""Walk the core file space and keep the :class:`FileIndex` current (ADR-0063).

The scan reads the :class:`~epicurus_core.files.FileStore` and writes only the DB index —
it never mutates the file space. It recurses ``list_dir`` rather than touching the
filesystem directly, so the same walk indexes a local-FS tree and an S3 bucket behind the
same contract (constraint #3). Entries seen are upserted; entries gone from the store are
purged, so a deleted file leaves search on the next pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatch

from epicurus_core import get_logger
from epicurus_core.files import FileStore
from epicurus_core_app.file_index import FileIndex

log = get_logger("core.file_scan")

_BATCH = 500  # flush to DB every N entries


def _excluded(rel_path: str, name: str, patterns: Sequence[str]) -> bool:
    """Whether *rel_path* (or its bare *name*) matches any exclude glob (#731).

    Matched against both forms so a pattern can target a whole subtree (``cache/*``,
    relative-path-shaped) or any file named that way regardless of where it sits
    (``*.tmp``, name-shaped) — whichever reads more naturally for the case at hand.
    """
    return any(fnmatch(rel_path, pat) or fnmatch(name, pat) for pat in patterns)


async def scan(
    store: FileStore,
    index: FileIndex,
    *,
    tenant: str,
    path_prefix: str = "",
    exclude: Sequence[str] = (),
) -> int:
    """Walk the file space via *store* and sync the DB *index*.

    Returns the total number of entries visited. The store's own root is not indexed itself
    (it has no name worth storing) — only its descendants, mirroring how a directory tree is
    browsed from the root down. *path_prefix* (#731) namespaces every indexed row — e.g.
    ``mount:<name>/`` for an external mount's own store, so its rows share the tenant's index
    table without colliding with the tenant tree or another mount, and *purge* only ever
    touches rows under that same prefix (never the tenant tree, never a sibling mount).
    *exclude* skips a matching entry, and does not descend into a matching directory.
    """
    log.info("file scan started", tenant=tenant, path_prefix=path_prefix)
    visited: set[str] = set()
    batch: list[dict[str, object]] = []
    # DFS over directories via the backend-agnostic listing; "" is the store's own root.
    stack: list[str] = [""]
    while stack:
        directory = stack.pop()
        children = await store.list_dir(tenant=tenant, path=directory)
        for entry in children:
            if exclude and _excluded(entry.path, entry.name, exclude):
                continue
            indexed_path = f"{path_prefix}{entry.path}"
            visited.add(indexed_path)
            batch.append(
                {
                    "path": indexed_path,
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

    deleted = await index.purge_stale(tenant=tenant, seen_paths=visited, path_prefix=path_prefix)
    total = len(visited)
    log.info("file scan complete", total=total, deleted=deleted, tenant=tenant)
    return total
