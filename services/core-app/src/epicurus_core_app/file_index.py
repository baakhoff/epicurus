"""The core-owned file index — tenant-scoped rows over the file space (ADR-0063).

Phase 2 of the file-space migration (ADR-0052) moves ownership of the unified Files view
into the core. This is its index: a tenant-scoped catalogue of every node in the
:class:`~epicurus_core.files.FileStore` tree, kept current by :mod:`file_scan` (a walk on
startup) and :mod:`file_watch` (incremental on change). It powers the core Files browser
page and name/path search, exactly as the storage module's own index used to — only now it
is the core, behind the swappable backend, that owns it (constraint #3).

Object-store uploads (chat attachments, agent-written objects) are **not** indexed here —
they live in the storage module's MinIO bucket, which storage still owns. The core Files
page merges them in live at render time (see :mod:`files_routes`); this index covers the
FileStore tree alone, so a backend swap (local-FS ↔ S3) carries the whole index with it.
"""

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
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

FileKind = Literal["file", "dir"]


def _like_prefix(path: str) -> str:
    r"""Escape SQL ``LIKE`` wildcards so a subtree match can't reach a sibling.

    ``_`` and ``%`` are ``LIKE`` metacharacters, yet both are legal in a stored path
    (``report_v2``, ``50%off``). Left unescaped, ``LIKE 'report_v2/%'`` also matches a sibling
    ``report-v2/…``. Here that only churns index rows — the #390 watcher re-indexes the sibling
    on its next pass — but it is the same latent bug the storage delete path carried before #574,
    so escape with a backslash to confine the match to the prefix and its true descendants;
    callers pair this with ``escape="\\"`` on ``.like(...)``. Order matters: escape ``\`` before
    the wildcards. Read-only :meth:`FileIndex.browse` needs no escaping — it re-filters to direct
    children in Python.
    """
    return path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class IndexedFile(BaseModel):
    """One row in the core file index, safe to return over the API."""

    path: str
    name: str
    size: int
    mtime: float
    kind: FileKind
    updated_at: datetime


class _Base(DeclarativeBase):
    pass


class _CoreFile(_Base):
    """ORM mapping for a single indexed file-space entry."""

    __tablename__ = "core_files"
    __table_args__ = (UniqueConstraint("tenant", "path", name="uq_core_files_tenant_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    path: Mapped[str] = mapped_column(String(4096))
    name: Mapped[str] = mapped_column(String(255))
    # File sizes can exceed 2 GB — BigInteger, never Integer (SQLite tolerates the overflow
    # in tests; Postgres INTEGER would overflow in production).
    size: Mapped[int] = mapped_column(BigInteger)
    mtime: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


def _row_to_entry(row: _CoreFile) -> IndexedFile:
    kind: FileKind = "dir" if row.kind == "dir" else "file"
    return IndexedFile(
        path=row.path,
        name=row.name,
        size=row.size,
        mtime=row.mtime,
        kind=kind,
        updated_at=row.updated_at,
    )


class FileIndex:
    """CRUD helpers for the tenant-scoped core file index in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema (idempotent — uses ``create_all``, no migration tool)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def upsert_batch(self, *, tenant: str, entries: list[dict[str, object]]) -> None:
        """Insert or update a batch of file entries (by path, per tenant).

        Each dict must have keys: path, name, size, mtime, kind. Uses a dialect-agnostic
        delete-then-insert so it works on both Postgres (production) and SQLite (tests).
        """
        if not entries:
            return
        paths = [str(e["path"]) for e in entries]
        async with self._session() as session:
            await session.execute(
                delete(_CoreFile).where(
                    _CoreFile.tenant == tenant,
                    _CoreFile.path.in_(paths),
                )
            )
            for e in entries:
                session.add(
                    _CoreFile(
                        tenant=tenant,
                        path=str(e["path"]),
                        name=str(e["name"]),
                        size=int(e["size"]),  # type: ignore[call-overload]
                        mtime=float(e["mtime"]),  # type: ignore[arg-type]
                        kind=str(e["kind"]),
                    )
                )
            await session.commit()

    async def get(self, *, tenant: str, path: str) -> IndexedFile | None:
        """Return the single entry at *path* for *tenant*, or ``None`` if absent."""
        path = path.replace("\\", "/").rstrip("/")
        async with self._session() as session:
            row = await session.scalar(
                select(_CoreFile).where(
                    _CoreFile.tenant == tenant,
                    _CoreFile.path == path,
                )
            )
            return None if row is None else _row_to_entry(row)

    async def purge_stale(self, *, tenant: str, seen_paths: set[str]) -> int:
        """Delete rows not visited in the most recent scan (files removed on disk)."""
        async with self._session() as session:
            result = await session.execute(
                delete(_CoreFile).where(
                    _CoreFile.tenant == tenant,
                    _CoreFile.path.not_in(seen_paths),
                )
            )
            await session.commit()
            deleted: int = cast("CursorResult[Any]", result).rowcount or 0
            return deleted

    async def remove_subtree(self, *, tenant: str, path: str) -> int:
        """Delete the row at *path* and every descendant under ``<path>/`` (#564).

        The de-index half of a Files-page delete: symmetric to :meth:`upsert_batch`, it keeps the
        index in step the moment a file or folder is removed, so the entry drops out of search and
        the listing at once (the #390 watcher stays the backstop). The empty-path root is a no-op.
        Returns the number of rows removed.
        """
        path = path.replace("\\", "/").rstrip("/")
        if not path:
            return 0
        async with self._session() as session:
            result = await session.execute(
                delete(_CoreFile).where(
                    _CoreFile.tenant == tenant,
                    or_(
                        _CoreFile.path == path,
                        _CoreFile.path.like(_like_prefix(path) + "/%", escape="\\"),
                    ),
                )
            )
            await session.commit()
            return cast("CursorResult[Any]", result).rowcount or 0

    async def browse(self, *, tenant: str, path: str) -> list[IndexedFile]:
        """List the direct children of *path* (empty string = root).

        *path* is a POSIX-style forward-slash relative path (the format used in the index).
        Directories sort before files, both by name.
        """
        path = path.replace("\\", "/").rstrip("/")
        async with self._session() as session:
            rows = await session.scalars(
                select(_CoreFile)
                .where(
                    _CoreFile.tenant == tenant,
                    _CoreFile.path.like((path + "/%") if path else "%"),
                )
                # Directories before files (the bool sorts False<True), then by name. Portable
                # across SQLite (0/1) and Postgres (false/true).
                .order_by(_CoreFile.kind == "file", _CoreFile.name)
            )
            all_rows = list(rows)

        # Keep only direct children: path has exactly one more segment than the prefix.
        prefix = path + "/" if path else ""
        result: list[IndexedFile] = []
        for row in all_rows:
            rel = row.path[len(prefix) :]
            if "/" not in rel and rel:
                result.append(_row_to_entry(row))
        return result

    async def count(self, *, tenant: str) -> dict[str, int]:
        """Return the number of indexed files and directories for *tenant*."""
        async with self._session() as session:
            files = await session.scalar(
                select(func.count()).where(_CoreFile.tenant == tenant, _CoreFile.kind == "file")
            )
            dirs_ = await session.scalar(
                select(func.count()).where(_CoreFile.tenant == tenant, _CoreFile.kind == "dir")
            )
        return {"files": int(files or 0), "dirs": int(dirs_ or 0)}

    async def search(self, *, tenant: str, query: str, limit: int = 50) -> list[IndexedFile]:
        """Full-path and name case-insensitive search."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_CoreFile)
                .where(
                    _CoreFile.tenant == tenant,
                    or_(
                        func.lower(_CoreFile.name).contains(query.lower()),
                        func.lower(_CoreFile.path).contains(query.lower()),
                    ),
                )
                .order_by(_CoreFile.kind == "file", _CoreFile.name)
                .limit(limit)
            )
            return [_row_to_entry(r) for r in rows]
