"""Walk the core file space and keep the :class:`FileIndex` current (ADR-0063).

The scan reads the :class:`~epicurus_core.files.FileStore` and writes only the DB index —
it never mutates the file space. It recurses ``list_dir`` rather than touching the
filesystem directly, so the same walk indexes a local-FS tree and an S3 bucket behind the
same contract (constraint #3). Entries seen are upserted; entries gone from the store are
purged, so a deleted file leaves search on the next pass.

That purge is the dangerous half (#848). A store that reads empty is indistinguishable
from a store whose contents were deleted, and on 2026-08-30 a stale bind mount made the
container see an empty ``/data`` while the real file space sat intact on the host — the
scan reconciled ``core_files`` to zero rows without a single error, and the knowledge
re-index that followed emptied its collection too. :class:`ScanFuse` restores the missing
distinction: a purge that would remove a wholesale share of the namespace's rows is
refused, the index is left untouched, and the refusal is logged, exported on ``/metrics``,
and readable at ``GET /platform/v1/files/scan-status`` until a clean scan re-arms it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch

from prometheus_client import Counter, Gauge

from epicurus_core import get_logger
from epicurus_core.files import FileStore
from epicurus_core_app.file_index import FileIndex

log = get_logger("core.file_scan")

_BATCH = 500  # flush to DB every N entries

# 1 while a namespace's fuse is tripped, back to 0 after a scan that does not trip it.
FUSE_TRIPPED = Gauge(
    "epicurus_core_file_scan_fuse_tripped",
    "1 while a file-space scan is refusing to purge index rows (mass de-index fuse).",
    ["tenant", "namespace"],
)
FUSE_TRIPS = Counter(
    "epicurus_core_file_scan_fuse_trips_total",
    "File-space index purges refused by the mass de-index fuse.",
    ["tenant", "namespace"],
)

# How a namespace with no prefix (the tenant tree itself) is labelled in metrics/logs.
_TENANT_TREE = "tenant"


def namespace_for(path_prefix: str = "") -> str:
    """The fuse's label for a scan namespace — the index prefix, or ``tenant`` for the tree.

    One function so the scan, the metric labels, and the HTTP surface can never disagree
    about what a namespace is called.
    """
    return path_prefix or _TENANT_TREE


@dataclass(frozen=True, slots=True)
class ScanFuseTrip:
    """A refused purge: what would have been removed from the index, and why it was not."""

    tenant: str
    namespace: str
    indexed_rows: int
    would_delete: int
    seen_entries: int
    reason: str
    at: str

    def as_dict(self) -> dict[str, object]:
        """The trip as a JSON-safe mapping (structured log fields, the HTTP payload)."""
        return {
            "tenant": self.tenant,
            "namespace": self.namespace,
            "indexed_rows": self.indexed_rows,
            "would_delete": self.would_delete,
            "seen_entries": self.seen_entries,
            "reason": self.reason,
            "at": self.at,
        }

    def summary(self) -> str:
        """One human sentence, for a log line or an operator-facing field."""
        return f"{self.namespace}: {self.reason} (at {self.at})"


class ScanFuse:
    """Thresholds and tripped state for the file-scan purge (#848).

    One instance is shared by every scan — the tenant tree and each external mount — and
    keys its state by ``(tenant, namespace)`` so a suspect mount never masks (or is masked
    by) a healthy tenant tree. Tenant scoping is structural, not incidental: the counts it
    weighs come from tenant-scoped queries, and every trip records the tenant it belongs to.

    Args:
        enabled: Master switch; disabled, :meth:`evaluate` never trips (pre-#848 behaviour).
        max_delete_ratio: Share of a namespace's indexed rows whose purge is suspect.
        min_deletions: Absolute floor below which the ratio rule never fires, so a small
            tree stays prunable. The empty-source rule ignores it: a store that lists
            *nothing* while rows exist is the stale-mount signature at any size.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_delete_ratio: float = 0.5,
        min_deletions: int = 5,
    ) -> None:
        self.enabled = enabled
        self.max_delete_ratio = max_delete_ratio
        self.min_deletions = min_deletions
        self._trips: dict[tuple[str, str], ScanFuseTrip] = {}

    @property
    def tripped(self) -> bool:
        """Whether any namespace is currently refusing to purge."""
        return bool(self._trips)

    def trips(self) -> list[ScanFuseTrip]:
        """Every namespace currently refusing to purge, tenant-scoped rows and all."""
        return list(self._trips.values())

    def evaluate(
        self,
        *,
        tenant: str,
        namespace: str,
        indexed_rows: int,
        would_delete: int,
        seen_entries: int,
        force: bool = False,
    ) -> ScanFuseTrip | None:
        """Judge one prospective purge; ``None`` means "go ahead". Side-effect free."""
        if force or not self.enabled:
            return None
        if indexed_rows <= 0 or would_delete <= 0:
            return None
        if seen_entries <= 0:
            reason = (
                f"file space read empty while {indexed_rows} row"
                f"{' is' if indexed_rows == 1 else 's are'} indexed; refusing to purge "
                "(suspect mount or unprovisioned root)"
            )
        else:
            ratio = would_delete / indexed_rows
            if would_delete < self.min_deletions or ratio < self.max_delete_ratio:
                return None
            reason = (
                f"scan would purge {would_delete} of {indexed_rows} indexed rows "
                f"({ratio:.0%} >= {self.max_delete_ratio:.0%} threshold); refusing to purge"
            )
        return ScanFuseTrip(
            tenant=tenant,
            namespace=namespace,
            indexed_rows=indexed_rows,
            would_delete=would_delete,
            seen_entries=seen_entries,
            reason=reason,
            at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def trip(self, trip: ScanFuseTrip) -> None:
        """Record a refusal: loud structured log, metrics, and the state the API reads."""
        self._trips[(trip.tenant, trip.namespace)] = trip
        FUSE_TRIPPED.labels(tenant=trip.tenant, namespace=trip.namespace).set(1)
        FUSE_TRIPS.labels(tenant=trip.tenant, namespace=trip.namespace).inc()
        log.error("mass de-index fuse tripped; index purge refused", **trip.as_dict())

    def clear(self, *, tenant: str, namespace: str) -> None:
        """Re-arm one namespace after a scan that completed without tripping it."""
        if self._trips.pop((tenant, namespace), None) is not None:
            log.info(
                "mass de-index fuse cleared; file space reads normally again",
                tenant=tenant,
                namespace=namespace,
            )
        FUSE_TRIPPED.labels(tenant=tenant, namespace=namespace).set(0)


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
    fuse: ScanFuse | None = None,
    force: bool = False,
) -> int:
    """Walk the file space via *store* and sync the DB *index*.

    Returns the total number of entries visited. The store's own root is not indexed itself
    (it has no name worth storing) — only its descendants, mirroring how a directory tree is
    browsed from the root down. *path_prefix* (#731) namespaces every indexed row — e.g.
    ``mount:<name>/`` for an external mount's own store, so its rows share the tenant's index
    table without colliding with the tenant tree or another mount, and *purge* only ever
    touches rows under that same prefix (never the tenant tree, never a sibling mount).
    *exclude* skips a matching entry, and does not descend into a matching directory.

    *fuse* (#848) guards the purge: when the rows this walk would delete look wholesale
    rather than editorial, the purge is skipped and the walk still returns its visited
    count — the upserts stand (they are additive and harmless), only the deletions are
    withheld, so a stale mount leaves a *stale* index instead of an emptied one. Passing
    ``None`` scans unguarded (the tests that assert convergence directly). *force* runs the
    purge regardless — the operator's "yes, the tree really is empty now".
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

    total = len(visited)
    namespace = namespace_for(path_prefix)
    if fuse is not None:
        trip = fuse.evaluate(
            tenant=tenant,
            namespace=namespace,
            indexed_rows=await index.count_rows(tenant=tenant, path_prefix=path_prefix),
            would_delete=await index.count_stale(
                tenant=tenant, seen_paths=visited, path_prefix=path_prefix
            ),
            seen_entries=total,
            force=force,
        )
        if trip is not None:
            fuse.trip(trip)
            log.warning(
                "file scan complete; purge refused by the fuse",
                total=total,
                tenant=tenant,
                namespace=namespace,
            )
            return total

    deleted = await index.purge_stale(tenant=tenant, seen_paths=visited, path_prefix=path_prefix)
    if fuse is not None:
        fuse.clear(tenant=tenant, namespace=namespace)
    log.info("file scan complete", total=total, deleted=deleted, tenant=tenant)
    return total
