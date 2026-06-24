"""Note store — the tenant-scoped source of truth for note bodies, in Postgres.

Unlike the knowledge module (which indexes an Obsidian vault that lives on disk),
notes are **authored in the app**, so their content must be externalized state, not
local disk (constraint #2). Postgres owns the note body; Qdrant only holds derived
vectors (see :mod:`epicurus_notes.indexer`).

A note is addressed by a tenant-unique ``slug`` (its stable id and the editor
``path``). ``title`` is derived from the body at save time and stored so the
document list and the attachment picker never have to read every body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    DateTime,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


@dataclass(frozen=True)
class NoteSummary:
    """A note without its body — for the document list and attachment picker."""

    slug: str
    title: str
    updated_at: datetime


@dataclass(frozen=True)
class NoteRecord:
    """A note with its full body — returned when one note is opened or resolved."""

    slug: str
    title: str
    content: str
    updated_at: datetime


class _Base(DeclarativeBase):
    pass


class _StoredNote(_Base):
    """ORM mapping for a single authored note (tenant-scoped)."""

    __tablename__ = "notes"
    __table_args__ = (UniqueConstraint("tenant", "slug", name="uq_notes_tenant_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    slug: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class _StoredNoteFolder(_Base):
    """ORM mapping for a note folder — a persisted tree directory (tenant-scoped).

    Notes are slug-keyed, so a folder is normally *implied* by a note slug that contains
    ``/``. This row makes a folder exist on its own — an **empty** folder the operator
    created in the editor — so it survives a reload before any note is filed under it.
    """

    __tablename__ = "note_folders"
    __table_args__ = (UniqueConstraint("tenant", "path", name="uq_note_folders_tenant_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    path: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NoteFolderStore:
    """CRUD for tenant-scoped note folders (the editor's empty/explicit tree directories)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema if it does not exist (shares ``NotesStore``'s metadata)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def list(self, *, tenant: str) -> list[str]:
        """Every explicitly-created folder path for *tenant*, lexicographically sorted.

        Sorted so a parent path always precedes its children (a prefix sorts before the
        longer string) — the editor's tree builder relies on dirs arriving parent-first.
        """
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredNoteFolder.path)
                .where(_StoredNoteFolder.tenant == tenant)
                .order_by(_StoredNoteFolder.path)
            )
            return list(rows)

    async def add(self, *, tenant: str, path: str) -> bool:
        """Create the folder *path* if absent; return whether a new row was inserted."""
        async with self._session() as session:
            existing = await session.scalar(
                select(_StoredNoteFolder).where(
                    _StoredNoteFolder.tenant == tenant,
                    _StoredNoteFolder.path == path,
                )
            )
            if existing is not None:
                return False
            session.add(_StoredNoteFolder(tenant=tenant, path=path))
            await session.commit()
            return True

    async def delete(self, *, tenant: str, path: str) -> bool:
        """Remove the folder row for *path*; returns whether a row was deleted."""
        async with self._session() as session:
            result = await session.execute(
                delete(_StoredNoteFolder).where(
                    _StoredNoteFolder.tenant == tenant,
                    _StoredNoteFolder.path == path,
                )
            )
            await session.commit()
            return bool(cast("Any", result).rowcount)


class NotesStore:
    """CRUD for the tenant-scoped notes table in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def list_summaries(self, *, tenant: str) -> list[NoteSummary]:
        """Every note for *tenant* (no bodies), most-recently-updated first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredNote)
                .where(_StoredNote.tenant == tenant)
                .order_by(_StoredNote.updated_at.desc(), _StoredNote.slug)
            )
            return [NoteSummary(slug=r.slug, title=r.title, updated_at=r.updated_at) for r in rows]

    async def list_all(self, *, tenant: str) -> list[NoteRecord]:
        """Every note for *tenant* **with** its body — used to backfill the .md mirror."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredNote).where(_StoredNote.tenant == tenant).order_by(_StoredNote.slug)
            )
            return [
                NoteRecord(slug=r.slug, title=r.title, content=r.content, updated_at=r.updated_at)
                for r in rows
            ]

    async def get(self, *, tenant: str, slug: str) -> NoteRecord | None:
        """The full note for *slug*, or ``None`` if it does not exist."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredNote).where(
                    _StoredNote.tenant == tenant,
                    _StoredNote.slug == slug,
                )
            )
            if row is None:
                return None
            return NoteRecord(
                slug=row.slug, title=row.title, content=row.content, updated_at=row.updated_at
            )

    async def upsert(self, *, tenant: str, slug: str, title: str, content: str) -> NoteRecord:
        """Create the note if new, else replace its title/body; return the saved note."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredNote).where(
                    _StoredNote.tenant == tenant,
                    _StoredNote.slug == slug,
                )
            )
            if row is None:
                row = _StoredNote(tenant=tenant, slug=slug, title=title, content=content)
                session.add(row)
            else:
                row.title = title
                row.content = content
            await session.commit()
            await session.refresh(row)
            return NoteRecord(
                slug=row.slug, title=row.title, content=row.content, updated_at=row.updated_at
            )

    async def delete(self, *, tenant: str, slug: str) -> bool:
        """Remove the note for *slug*; returns whether a row was deleted."""
        async with self._session() as session:
            result = await session.execute(
                delete(_StoredNote).where(
                    _StoredNote.tenant == tenant,
                    _StoredNote.slug == slug,
                )
            )
            await session.commit()
            # delete() returns a CursorResult (has rowcount); the async stub widens
            # it to Result, so narrow it back to read the affected-row count.
            return bool(cast("Any", result).rowcount)

    async def count(self, *, tenant: str) -> int:
        """Number of notes for *tenant*."""
        async with self._session() as session:
            result = await session.scalar(
                select(func.count(_StoredNote.id)).where(_StoredNote.tenant == tenant)
            )
            return int(result) if result is not None else 0

    async def last_updated_at(self, *, tenant: str) -> str | None:
        """ISO-8601 timestamp of the most recently saved note for *tenant*, or None."""
        async with self._session() as session:
            result = await session.scalar(
                select(func.max(_StoredNote.updated_at)).where(_StoredNote.tenant == tenant)
            )
            return result.isoformat() if result is not None else None
