"""File-index schema and query helpers — tenant-scoped rows in Postgres."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    String,
    UniqueConstraint,
    delete,
    func,
    or_,
    select,
)
from sqlalchemy.engine import Connection, CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns

FileKind = Literal["file", "dir"]

# Where an entry's bytes live: the read-only filesystem scan ("fs") or the writable
# MinIO object store ("object", e.g. chat uploads — ADR-0025). Only "fs" rows are
# purged by a directory rescan; object rows persist until explicitly removed.
FileSource = Literal["fs", "object"]

# Columns added to storage_files after its first release. On an existing deployment
# these are added in place at init (the index uses ``create_all``, no migration tool).
_ADDED_COLUMNS = ("source",)


class FileEntry(BaseModel):
    """One row in the file index, safe to return over the API."""

    path: str
    name: str
    size: int
    mtime: float
    kind: FileKind
    updated_at: datetime
    # Backing store for this entry's bytes; "fs" for scanned files, "object" for uploads.
    source: FileSource = "fs"


class _Base(DeclarativeBase):
    pass


class _StoredFile(_Base):
    """ORM mapping for a single indexed filesystem entry."""

    __tablename__ = "storage_files"
    __table_args__ = (UniqueConstraint("tenant", "path", name="uq_storage_tenant_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    path: Mapped[str] = mapped_column(String(4096))
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger)
    mtime: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # "fs" (scanned, read-only) or "object" (MinIO-backed upload). Defaults to "fs"
    # so existing rows and the scanner need no change; a rescan only purges "fs".
    # ``server_default`` is raw SQL, hence the quoted literal.
    source: Mapped[str] = mapped_column(String(16), server_default="'fs'", default="fs")


def _row_to_entry(row: _StoredFile) -> FileEntry:
    kind: FileKind = "dir" if row.kind == "dir" else "file"
    source: FileSource = "object" if row.source == "object" else "fs"
    return FileEntry(
        path=row.path,
        name=row.name,
        size=row.size,
        mtime=row.mtime,
        kind=kind,
        updated_at=row.updated_at,
        source=source,
    )


class FileIndex:
    """CRUD helpers for the tenant-scoped file index in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Reconcile columns added after first release via the shared additive helper (#249).

        ``source`` (#KB-refactor) carries a ``server_default`` of ``'fs'``, so the helper
        adds it ``NOT NULL DEFAULT 'fs'`` — backfilling existing rows. See
        :func:`epicurus_core.db.ensure_columns`.
        """
        ensure_columns(sync_conn, _StoredFile.__table__, _ADDED_COLUMNS)

    async def upsert_batch(
        self,
        *,
        tenant: str,
        entries: list[dict[str, object]],
        source: FileSource = "fs",
    ) -> None:
        """Insert or update a batch of file entries (by path, per tenant).

        Each dict must have keys: path, name, size, mtime, kind. ``source`` marks where
        the bytes live — the scanner passes the default ``"fs"``; the upload sink passes
        ``"object"`` so a rescan's :meth:`purge_stale` leaves the rows alone.
        Uses a dialect-agnostic delete-then-insert approach so it works on
        both Postgres (production) and SQLite (tests).
        """
        if not entries:
            return
        paths = [str(e["path"]) for e in entries]
        async with self._session() as session:
            await session.execute(
                delete(_StoredFile).where(
                    _StoredFile.tenant == tenant,
                    _StoredFile.path.in_(paths),
                )
            )
            for e in entries:
                session.add(
                    _StoredFile(
                        tenant=tenant,
                        path=str(e["path"]),
                        name=str(e["name"]),
                        size=int(e["size"]),  # type: ignore[call-overload]
                        mtime=float(e["mtime"]),  # type: ignore[arg-type]
                        kind=str(e["kind"]),
                        source=source,
                    )
                )
            await session.commit()

    async def get(self, *, tenant: str, path: str) -> FileEntry | None:
        """Return the single entry at *path* for *tenant*, or ``None`` if absent.

        Used by the download endpoint to route by ``source`` — an "object" entry is
        streamed from MinIO, an "fs" entry from the read-only tree.
        """
        path = path.replace("\\", "/").rstrip("/")
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredFile).where(
                    _StoredFile.tenant == tenant,
                    _StoredFile.path == path,
                )
            )
            return None if row is None else _row_to_entry(row)

    async def purge_stale(self, *, tenant: str, seen_paths: set[str]) -> int:
        """Delete stale **filesystem** rows not visited in the most recent scan.

        Scoped to ``source == "fs"`` so MinIO-backed upload rows (which the scanner
        never visits) survive every rescan.
        """
        async with self._session() as session:
            result = await session.execute(
                delete(_StoredFile).where(
                    _StoredFile.tenant == tenant,
                    _StoredFile.source == "fs",
                    _StoredFile.path.not_in(seen_paths),
                )
            )
            await session.commit()
            deleted: int = cast("CursorResult[Any]", result).rowcount or 0
            return deleted

    async def browse(self, *, tenant: str, path: str) -> list[FileEntry]:
        """List the direct children of *path* (empty string = root).

        *path* is always a POSIX-style forward-slash relative path (same format
        used in the index).  An empty string browses the root.
        """
        # Normalise: strip trailing slash, replace any back-slashes from callers.
        path = path.replace("\\", "/").rstrip("/")
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredFile)
                .where(
                    _StoredFile.tenant == tenant,
                    _StoredFile.path.like((path + "/%") if path else "%"),
                )
                .order_by(_StoredFile.kind.desc(), _StoredFile.name)
            )
            all_rows = list(rows)

        # Keep only direct children: path has exactly one more segment than the prefix.
        prefix = path + "/" if path else ""
        result: list[FileEntry] = []
        for row in all_rows:
            rel = row.path[len(prefix) :]
            if "/" not in rel and rel:
                result.append(_row_to_entry(row))
        return result

    async def count(self, *, tenant: str) -> dict[str, int]:
        """Return the number of indexed files and directories for *tenant*."""
        async with self._session() as session:
            files = await session.scalar(
                select(func.count()).where(_StoredFile.tenant == tenant, _StoredFile.kind == "file")
            )
            dirs_ = await session.scalar(
                select(func.count()).where(_StoredFile.tenant == tenant, _StoredFile.kind == "dir")
            )
        return {"files": int(files or 0), "dirs": int(dirs_ or 0)}

    async def search(self, *, tenant: str, query: str, limit: int = 50) -> list[FileEntry]:
        """Full-path and name case-insensitive search."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredFile)
                .where(
                    _StoredFile.tenant == tenant,
                    or_(
                        func.lower(_StoredFile.name).contains(query.lower()),
                        func.lower(_StoredFile.path).contains(query.lower()),
                    ),
                )
                .order_by(_StoredFile.kind.desc(), _StoredFile.name)
                .limit(limit)
            )
            return [_row_to_entry(r) for r in rows]

    async def subtree(self, *, tenant: str, path: str) -> list[FileEntry]:
        """Return the entry at *path* plus every descendant under ``<path>/``.

        Used by move/rename (#391) to validate a whole subtree is writable and that the
        destination is free. Empty *path* (the root) returns nothing — the root never moves.
        """
        path = path.replace("\\", "/").rstrip("/")
        if not path:
            return []
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredFile)
                .where(
                    _StoredFile.tenant == tenant,
                    or_(_StoredFile.path == path, _StoredFile.path.like(path + "/%")),
                )
                .order_by(_StoredFile.path)
            )
            return [_row_to_entry(r) for r in rows]

    async def repath(self, *, tenant: str, src: str, dst: str) -> None:
        """Re-key the subtree rooted at *src* to *dst*, in one transaction (#391).

        Rewrites ``path`` for the root row and every descendant; the root row also gets its
        display ``name`` updated. The caller has already checked the subtree is writable and
        the destination free, so a single commit moves the tree atomically — a listing never
        sees it half-moved.
        """
        src = src.replace("\\", "/").rstrip("/")
        dst = dst.replace("\\", "/").rstrip("/")
        async with self._session() as session:
            rows = list(
                await session.scalars(
                    select(_StoredFile).where(
                        _StoredFile.tenant == tenant,
                        or_(_StoredFile.path == src, _StoredFile.path.like(src + "/%")),
                    )
                )
            )
            for row in rows:
                row.path = dst + row.path[len(src) :]
                if row.path == dst:  # the moved root carries a new display name
                    row.name = dst.rsplit("/", 1)[-1]
            await session.commit()

    async def delete_subtree(self, *, tenant: str, path: str) -> int:
        """Drop the row at *path* and every descendant under ``<path>/`` (#564).

        The index half of a delete: symmetric to :meth:`repath`, it removes the whole subtree
        in one commit so a listing never sees it half-gone. The MinIO byte deletes are the
        caller's job (:func:`~epicurus_storage.service.delete_item`). Empty *path* (the root)
        removes nothing. Returns the number of rows deleted.
        """
        path = path.replace("\\", "/").rstrip("/")
        if not path:
            return 0
        async with self._session() as session:
            result = await session.execute(
                delete(_StoredFile).where(
                    _StoredFile.tenant == tenant,
                    or_(_StoredFile.path == path, _StoredFile.path.like(path + "/%")),
                )
            )
            await session.commit()
            return cast("CursorResult[Any]", result).rowcount or 0
