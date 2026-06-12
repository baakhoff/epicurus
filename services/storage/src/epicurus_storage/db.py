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
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

FileKind = Literal["file", "dir"]


class FileEntry(BaseModel):
    """One row in the file index, safe to return over the API."""

    path: str
    name: str
    size: int
    mtime: float
    kind: FileKind
    updated_at: datetime


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


def _row_to_entry(row: _StoredFile) -> FileEntry:
    kind: FileKind = "dir" if row.kind == "dir" else "file"
    return FileEntry(
        path=row.path,
        name=row.name,
        size=row.size,
        mtime=row.mtime,
        kind=kind,
        updated_at=row.updated_at,
    )


class FileIndex:
    """CRUD helpers for the tenant-scoped file index in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def upsert_batch(
        self,
        *,
        tenant: str,
        entries: list[dict[str, object]],
    ) -> None:
        """Insert or update a batch of file entries (by path, per tenant).

        Each dict must have keys: path, name, size, mtime, kind.
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
                    )
                )
            await session.commit()

    async def purge_stale(self, *, tenant: str, seen_paths: set[str]) -> int:
        """Delete rows whose paths were not visited in the most recent scan."""
        async with self._session() as session:
            result = await session.execute(
                delete(_StoredFile).where(
                    _StoredFile.tenant == tenant,
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
