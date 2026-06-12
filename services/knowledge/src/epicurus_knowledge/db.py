"""Note-index schema — tenant-scoped tracking of vault notes in Postgres.

Each row records the note's last-seen mtime and sha-256 content hash so the
indexer can skip unchanged notes on subsequent runs.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class NoteRecord:
    """Projection returned by NoteIndex queries — immutable value object."""

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


class _Base(DeclarativeBase):
    pass


class _StoredNote(_Base):
    """ORM mapping for a single indexed vault note."""

    __tablename__ = "knowledge_notes"
    __table_args__ = (UniqueConstraint("tenant", "note_path", name="uq_knowledge_tenant_note"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    note_path: Mapped[str] = mapped_column(String(4096))
    # Modification time in nanoseconds for precise change detection.
    mtime_ns: Mapped[int] = mapped_column(Integer)
    # SHA-256 hex digest of the note's raw bytes.
    content_hash: Mapped[str] = mapped_column(String(64))
    # Number of chunks currently indexed in Qdrant for this note.
    chunk_count: Mapped[int] = mapped_column(Integer)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NoteIndex:
    """CRUD helpers for the tenant-scoped note index in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

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

    async def list_paths(self, *, tenant: str) -> list[str]:
        """Return all indexed note paths for *tenant*."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredNote.note_path).where(_StoredNote.tenant == tenant)
            )
            return list(rows)
