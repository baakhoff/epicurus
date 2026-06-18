"""File-index schema — tenant-scoped tracking of markdown sources in Postgres.

Two tables exist with identical structure:

* ``knowledge_notes`` — the operator's Obsidian vault.
* ``knowledge_doc_index`` — the bundled platform docs (self-documentation, #83).

Each row records the file's last-seen mtime and sha-256 content hash so the
indexer can skip unchanged files on subsequent runs.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class NoteRecord:
    """Projection returned by index queries — immutable value object."""

    __slots__ = ("chunk_count", "content_hash", "mtime_ns", "note_path")

    def __init__(
        self,
        note_path: str,
        mtime_ns: int,
        content_hash: str,
        chunk_count: int,
    ) -> None:
        self.note_path = note_path
        self.mtime_ns = mtime_ns
        self.content_hash = content_hash
        self.chunk_count = chunk_count


# ── Vault notes (operator's Obsidian vault) ──────────────────────────────────


class _NoteBase(DeclarativeBase):
    pass


class _StoredNote(_NoteBase):
    """ORM mapping for a single indexed vault note."""

    __tablename__ = "knowledge_notes"
    __table_args__ = (UniqueConstraint("tenant", "note_path", name="uq_knowledge_tenant_note"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    note_path: Mapped[str] = mapped_column(String(4096))
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NoteIndex:
    """CRUD helpers for the tenant-scoped vault note index in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_NoteBase.metadata.create_all)

    async def get(self, *, tenant: str, note_path: str) -> NoteRecord | None:
        """Return the existing record for *note_path*, or ``None``."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredNote).where(
                    _StoredNote.tenant == tenant,
                    _StoredNote.note_path == note_path,
                )
            )
            if row is None:
                return None
            return NoteRecord(
                note_path=row.note_path,
                mtime_ns=row.mtime_ns,
                content_hash=row.content_hash,
                chunk_count=row.chunk_count,
            )

    async def upsert(
        self,
        *,
        tenant: str,
        note_path: str,
        mtime_ns: int,
        content_hash: str,
        chunk_count: int,
    ) -> None:
        """Insert or replace the record for *note_path*."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredNote).where(
                    _StoredNote.tenant == tenant,
                    _StoredNote.note_path == note_path,
                )
            )
            session.add(
                _StoredNote(
                    tenant=tenant,
                    note_path=note_path,
                    mtime_ns=mtime_ns,
                    content_hash=content_hash,
                    chunk_count=chunk_count,
                )
            )
            await session.commit()

    async def delete(self, *, tenant: str, note_path: str) -> None:
        """Remove the record for *note_path* (note was deleted from the vault)."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredNote).where(
                    _StoredNote.tenant == tenant,
                    _StoredNote.note_path == note_path,
                )
            )
            await session.commit()

    async def clear(self, *, tenant: str) -> None:
        """Delete every record for *tenant*.

        Used to self-heal after the Qdrant vectors are reset (#229): the ledger must
        forget what it thinks is indexed so the next run re-embeds from scratch.
        """
        async with self._session() as session:
            await session.execute(delete(_StoredNote).where(_StoredNote.tenant == tenant))
            await session.commit()

    async def list_paths(self, *, tenant: str) -> list[str]:
        """Return all indexed note paths for *tenant*."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredNote.note_path).where(_StoredNote.tenant == tenant)
            )
            return list(rows)

    async def count(self, *, tenant: str) -> int:
        """Return the number of indexed notes for *tenant*."""
        async with self._session() as session:
            result = await session.scalar(
                select(func.count(_StoredNote.id)).where(_StoredNote.tenant == tenant)
            )
            return int(result) if result is not None else 0

    async def last_indexed_at(self, *, tenant: str) -> str | None:
        """Return the ISO-8601 timestamp of the most recent index run for *tenant*, or None."""
        async with self._session() as session:
            result = await session.scalar(
                select(func.max(_StoredNote.indexed_at)).where(_StoredNote.tenant == tenant)
            )
            return result.isoformat() if result is not None else None

    async def indexed_at(self, *, tenant: str, note_path: str) -> str | None:
        """Return the ISO-8601 timestamp *note_path* was last indexed, or None if unknown."""
        async with self._session() as session:
            result = await session.scalar(
                select(_StoredNote.indexed_at).where(
                    _StoredNote.tenant == tenant,
                    _StoredNote.note_path == note_path,
                )
            )
            return result.isoformat() if result is not None else None


# ── Platform docs (self-documentation, #83) ──────────────────────────────────


class _DocBase(DeclarativeBase):
    pass


class _StoredDoc(_DocBase):
    """ORM mapping for a single indexed platform-docs page."""

    __tablename__ = "knowledge_doc_index"
    __table_args__ = (UniqueConstraint("tenant", "note_path", name="uq_knowledge_doc_tenant_note"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    note_path: Mapped[str] = mapped_column(String(4096))
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DocIndex:
    """CRUD helpers for the tenant-scoped platform-docs index in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_DocBase.metadata.create_all)

    async def get(self, *, tenant: str, note_path: str) -> NoteRecord | None:
        """Return the existing record for *note_path*, or ``None``."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredDoc).where(
                    _StoredDoc.tenant == tenant,
                    _StoredDoc.note_path == note_path,
                )
            )
            if row is None:
                return None
            return NoteRecord(
                note_path=row.note_path,
                mtime_ns=row.mtime_ns,
                content_hash=row.content_hash,
                chunk_count=row.chunk_count,
            )

    async def upsert(
        self,
        *,
        tenant: str,
        note_path: str,
        mtime_ns: int,
        content_hash: str,
        chunk_count: int,
    ) -> None:
        """Insert or replace the record for *note_path*."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredDoc).where(
                    _StoredDoc.tenant == tenant,
                    _StoredDoc.note_path == note_path,
                )
            )
            session.add(
                _StoredDoc(
                    tenant=tenant,
                    note_path=note_path,
                    mtime_ns=mtime_ns,
                    content_hash=content_hash,
                    chunk_count=chunk_count,
                )
            )
            await session.commit()

    async def delete(self, *, tenant: str, note_path: str) -> None:
        """Remove the record for *note_path*."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredDoc).where(
                    _StoredDoc.tenant == tenant,
                    _StoredDoc.note_path == note_path,
                )
            )
            await session.commit()

    async def clear(self, *, tenant: str) -> None:
        """Delete every record for *tenant* (self-heal after a Qdrant reset, #229)."""
        async with self._session() as session:
            await session.execute(delete(_StoredDoc).where(_StoredDoc.tenant == tenant))
            await session.commit()

    async def list_paths(self, *, tenant: str) -> list[str]:
        """Return all indexed doc paths for *tenant*."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredDoc.note_path).where(_StoredDoc.tenant == tenant)
            )
            return list(rows)

    async def count(self, *, tenant: str) -> int:
        """Return the number of indexed docs for *tenant*."""
        async with self._session() as session:
            result = await session.scalar(
                select(func.count(_StoredDoc.id)).where(_StoredDoc.tenant == tenant)
            )
            return int(result) if result is not None else 0

    async def last_indexed_at(self, *, tenant: str) -> str | None:
        """Return the ISO-8601 timestamp of the most recent index run for *tenant*, or None."""
        async with self._session() as session:
            result = await session.scalar(
                select(func.max(_StoredDoc.indexed_at)).where(_StoredDoc.tenant == tenant)
            )
            return result.isoformat() if result is not None else None

    async def indexed_at(self, *, tenant: str, note_path: str) -> str | None:
        """Return the ISO-8601 timestamp *note_path* was last indexed, or None if unknown."""
        async with self._session() as session:
            result = await session.scalar(
                select(_StoredDoc.indexed_at).where(
                    _StoredDoc.tenant == tenant,
                    _StoredDoc.note_path == note_path,
                )
            )
            return result.isoformat() if result is not None else None
