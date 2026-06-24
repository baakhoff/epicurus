"""File-index schema — tenant-scoped tracking of markdown sources in Postgres.

Two tables track the markdown sources with identical structure:

* ``knowledge_notes`` — the operator's Obsidian vault.
* ``knowledge_doc_index`` — the bundled platform docs (self-documentation, #83).

Each row records the file's last-seen mtime and sha-256 content hash so the
indexer can skip unchanged files on subsequent runs.

A third table — ``knowledge_versions`` — keeps a content snapshot per editor save
(version history, #ADR-0046), so the editor can list and re-open a document's past
revisions. It shares this metadata's engine and is created by :class:`VersionStore`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
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


# ── Document version history (editor save snapshots, #ADR-0046) ───────────────


# Per (tenant, note_path) versions kept; older ones are pruned after each new save so a
# heavily-edited document can't grow the table without bound.
MAX_VERSIONS = 50


class VersionRecord:
    """Projection returned by version queries — an immutable value object.

    ``content`` is ``None`` for list rows (the bodies are not loaded when listing) and
    populated for a single fetched version; ``size`` is the snapshot's character count.
    """

    __slots__ = ("content", "created_at", "note_path", "size", "title", "version_id")

    def __init__(
        self,
        version_id: str,
        note_path: str,
        title: str,
        created_at: datetime,
        size: int,
        content: str | None = None,
    ) -> None:
        self.version_id = version_id
        self.note_path = note_path
        self.title = title
        self.created_at = created_at
        self.size = size
        self.content = content


class _VersionBase(DeclarativeBase):
    pass


class _StoredVersion(_VersionBase):
    """ORM mapping for a single saved snapshot of a vault document."""

    __tablename__ = "knowledge_versions"
    __table_args__ = (Index("ix_knowledge_versions_tenant_path", "tenant", "note_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    note_path: Mapped[str] = mapped_column(String(4096))
    title: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VersionStore:
    """Tenant-scoped content-snapshot history for vault documents (editor saves, #ADR-0046).

    Backed by the same :class:`~sqlalchemy.ext.asyncio.AsyncEngine` as the index ledgers.
    Each editor save records one snapshot (deduplicated against the previous one); the
    newest :data:`MAX_VERSIONS` per ``(tenant, note_path)`` are retained.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_VersionBase.metadata.create_all)

    async def add_version(self, *, tenant: str, note_path: str, title: str, content: str) -> None:
        """Snapshot *content* for ``(tenant, note_path)``.

        Dedup: if the newest existing snapshot is byte-identical, no row is written (an
        idle/blur auto-save that didn't change anything must not pile up duplicates).
        After inserting, retention prunes everything beyond the newest
        :data:`MAX_VERSIONS` for this ``(tenant, note_path)``.
        """
        async with self._session() as session:
            newest = await session.scalar(
                select(_StoredVersion.content)
                .where(
                    _StoredVersion.tenant == tenant,
                    _StoredVersion.note_path == note_path,
                )
                .order_by(_StoredVersion.id.desc())
                .limit(1)
            )
            if newest == content:
                return
            session.add(
                _StoredVersion(
                    tenant=tenant,
                    note_path=note_path,
                    title=title,
                    content=content,
                )
            )
            await session.commit()
            # Retention: keep only the newest MAX_VERSIONS snapshots for this document.
            keep_ids = (
                await session.scalars(
                    select(_StoredVersion.id)
                    .where(
                        _StoredVersion.tenant == tenant,
                        _StoredVersion.note_path == note_path,
                    )
                    .order_by(_StoredVersion.id.desc())
                    .limit(MAX_VERSIONS)
                )
            ).all()
            await session.execute(
                delete(_StoredVersion).where(
                    _StoredVersion.tenant == tenant,
                    _StoredVersion.note_path == note_path,
                    _StoredVersion.id.notin_(keep_ids),
                )
            )
            await session.commit()

    async def list_versions(self, *, tenant: str, note_path: str) -> list[VersionRecord]:
        """Return this document's snapshots, newest first (no bodies loaded)."""
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(
                        _StoredVersion.id,
                        _StoredVersion.title,
                        _StoredVersion.created_at,
                        func.length(_StoredVersion.content),
                    )
                    .where(
                        _StoredVersion.tenant == tenant,
                        _StoredVersion.note_path == note_path,
                    )
                    .order_by(_StoredVersion.id.desc())
                )
            ).all()
            return [
                VersionRecord(
                    version_id=str(row[0]),
                    note_path=note_path,
                    title=row[1],
                    created_at=row[2],
                    size=int(row[3]) if row[3] is not None else 0,
                )
                for row in rows
            ]

    async def get_version(
        self, *, tenant: str, note_path: str, version_id: str
    ) -> VersionRecord | None:
        """Return the full snapshot for *version_id*, or ``None`` if it does not exist.

        A non-integer ``version_id`` is *not found*, not an error — it reaches us only
        through a client-supplied query parameter, so it is never trusted.
        """
        try:
            pk = int(version_id)
        except (TypeError, ValueError):
            return None
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredVersion).where(
                    _StoredVersion.id == pk,
                    _StoredVersion.tenant == tenant,
                    _StoredVersion.note_path == note_path,
                )
            )
            if row is None:
                return None
            return VersionRecord(
                version_id=str(row.id),
                note_path=row.note_path,
                title=row.title,
                created_at=row.created_at,
                size=len(row.content),
                content=row.content,
            )
