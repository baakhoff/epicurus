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
    Index,
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


@dataclass(frozen=True)
class VersionSummary:
    """One past version without its body — for the version list (ADR-0045)."""

    slug: str
    version_id: str
    created_at: datetime
    title: str
    size: int


@dataclass(frozen=True)
class VersionRecord:
    """A past version with its full body — returned when one version is fetched."""

    slug: str
    version_id: str
    created_at: datetime
    title: str
    content: str


# Cap on retained versions per (tenant, slug): the list never exceeds this and a
# save prunes anything older (ADR-0045).
MAX_VERSIONS = 50


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


class _NoteVersion(_Base):
    """An immutable snapshot of a note's body, recorded on each save (ADR-0045).

    Tenant-scoped and indexed on ``(tenant, slug)`` so the version list and prune
    are cheap. The row ``id`` is the opaque ``version_id`` clients use to fetch a
    snapshot back.
    """

    __tablename__ = "note_versions"
    __table_args__ = (Index("ix_note_versions_tenant_slug", "tenant", "slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    slug: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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

    # ── version history (ADR-0045) ────────────────────────────────────────────

    async def add_version(self, *, tenant: str, slug: str, title: str, content: str) -> None:
        """Snapshot *content* for (tenant, slug), unless it is unchanged.

        Dedups against the newest existing version (a byte-identical re-save adds
        nothing) and, after inserting, prunes anything older than the newest
        :data:`MAX_VERSIONS` rows so a note's history is bounded.
        """
        async with self._session() as session:
            latest = await session.scalar(
                select(_NoteVersion)
                .where(_NoteVersion.tenant == tenant, _NoteVersion.slug == slug)
                .order_by(_NoteVersion.id.desc())
                .limit(1)
            )
            if latest is not None and latest.content == content:
                return
            session.add(_NoteVersion(tenant=tenant, slug=slug, title=title, content=content))
            await session.commit()
            await self._prune_versions(session, tenant=tenant, slug=slug)

    async def _prune_versions(self, session: Any, *, tenant: str, slug: str) -> None:
        """Delete every version for (tenant, slug) older than the newest MAX_VERSIONS."""
        keep_ids = (
            select(_NoteVersion.id)
            .where(_NoteVersion.tenant == tenant, _NoteVersion.slug == slug)
            .order_by(_NoteVersion.id.desc())
            .limit(MAX_VERSIONS)
            .scalar_subquery()
        )
        await session.execute(
            delete(_NoteVersion).where(
                _NoteVersion.tenant == tenant,
                _NoteVersion.slug == slug,
                _NoteVersion.id.notin_(keep_ids),
            )
        )
        await session.commit()

    async def list_versions(self, *, tenant: str, slug: str) -> list[VersionSummary]:
        """Past versions for (tenant, slug), newest first, capped at MAX_VERSIONS (no bodies)."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_NoteVersion)
                .where(_NoteVersion.tenant == tenant, _NoteVersion.slug == slug)
                .order_by(_NoteVersion.id.desc())
                .limit(MAX_VERSIONS)
            )
            return [
                VersionSummary(
                    slug=r.slug,
                    version_id=str(r.id),
                    created_at=r.created_at,
                    title=r.title,
                    size=len(r.content),
                )
                for r in rows
            ]

    async def get_version(self, *, tenant: str, slug: str, version_id: str) -> VersionRecord | None:
        """The full version for *version_id*, or None if it is not this tenant+slug's.

        A non-integer ``version_id`` (clients treat it as opaque) resolves to None
        rather than erroring.
        """
        try:
            pk = int(version_id)
        except (TypeError, ValueError):
            return None
        async with self._session() as session:
            row = await session.scalar(
                select(_NoteVersion).where(
                    _NoteVersion.id == pk,
                    _NoteVersion.tenant == tenant,
                    _NoteVersion.slug == slug,
                )
            )
            if row is None:
                return None
            return VersionRecord(
                slug=row.slug,
                version_id=str(row.id),
                created_at=row.created_at,
                title=row.title,
                content=row.content,
            )
