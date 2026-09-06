"""Durable, tenant-scoped rows for export and import jobs (#867).

Both halves of portability are long-running background work over a shared, disposable
staging directory, and both are driven from a browser tab that can be reloaded, closed, or
opened on a second device mid-run. So the job cannot live in process memory: a page reload
must be able to find the export it started and the archive it produced, and an apply must
be able to say what it did after the request that launched it is long gone.

The staged archive itself is *not* in here — it is a file in a disposable cache directory
(constraint #2 allows exactly that, and only that). This table holds the row that points at
it, so a job whose staging file has been swept reports honestly instead of silently
resurrecting.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import BigInteger, DateTime, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

__all__ = ["JobKind", "PortabilityJob", "PortabilityJobStore"]

JobKind = Literal["export", "import"]


class _Base(DeclarativeBase):
    pass


class _PortabilityJobRow(_Base):
    """One export or import job, tenant-scoped."""

    __tablename__ = "portability_jobs"

    # A client-opaque uuid rather than an autoincrement: the id appears in the archive
    # download URL, and a guessable sequential id would let one tenant's URL be walked
    # into another's (the row is tenant-filtered too — this is the belt to that's braces).
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Per-component progress (export) — a list of ComponentEntry mappings.
    progress: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    preview: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Absolute path of the staged archive in the disposable staging directory.
    archive_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # An archive is a file, and files exceed 2 GB — BigInteger, never Integer (AGENTS.md:
    # SQLite tolerates the overflow in tests, Postgres INTEGER does not in production).
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PortabilityJob:
    """A snapshot of one job row, detached from the session that read it."""

    __slots__ = (
        "archive_path",
        "created_at",
        "error",
        "id",
        "kind",
        "manifest",
        "preview",
        "progress",
        "report",
        "size_bytes",
        "status",
        "tenant",
        "updated_at",
    )

    def __init__(self, row: _PortabilityJobRow) -> None:
        self.id = row.id
        self.tenant = row.tenant
        self.kind: JobKind = "export" if row.kind == "export" else "import"
        self.status = row.status
        self.created_at = row.created_at
        self.updated_at = row.updated_at
        self.progress = list(row.progress or [])
        self.manifest = row.manifest
        self.preview = row.preview
        self.report = row.report
        self.archive_path = row.archive_path
        self.size_bytes = int(row.size_bytes or 0)
        self.error = row.error


class PortabilityJobStore:
    """CRUD for the tenant-scoped portability job rows."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema (idempotent — ``create_all``, no migration tool)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def create(self, *, tenant: str, kind: JobKind, status: str) -> PortabilityJob:
        """Open a new job and return it."""
        row = _PortabilityJobRow(
            id=str(uuid.uuid4()),
            tenant=tenant,
            kind=kind,
            status=status,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            progress=[],
            size_bytes=0,
        )
        async with self._session() as session:
            session.add(row)
            await session.commit()
        return PortabilityJob(row)

    async def get(self, *, tenant: str, job_id: str) -> PortabilityJob | None:
        """One job, scoped to *tenant* — a foreign id reads as absent, never as another's."""
        async with self._session() as session:
            row = await session.scalar(
                select(_PortabilityJobRow).where(
                    _PortabilityJobRow.tenant == tenant,
                    _PortabilityJobRow.id == job_id,
                )
            )
            return None if row is None else PortabilityJob(row)

    async def update(self, *, tenant: str, job_id: str, **fields: Any) -> None:
        """Patch the named columns of one job (no-op if it has been swept)."""
        async with self._session() as session:
            row = await session.scalar(
                select(_PortabilityJobRow).where(
                    _PortabilityJobRow.tenant == tenant,
                    _PortabilityJobRow.id == job_id,
                )
            )
            if row is None:
                return
            for name, value in fields.items():
                setattr(row, name, value)
            row.updated_at = datetime.now(UTC)
            await session.commit()

    async def recent(self, *, tenant: str, limit: int = 20) -> list[PortabilityJob]:
        """This tenant's most recent jobs, newest first, capped at *limit* (#877).

        The read a reloaded browser tab makes: the card holds a job id only for the life of
        the page, so without this an export started a minute ago is unreachable and its
        archive is swept without ever being offered. Tenant-filtered like every other read
        here — one tenant never learns that another's job exists.

        Ordered by ``created_at`` with the id as a tiebreak, so two jobs opened in the same
        microsecond still come back in a stable order rather than the storage engine's.
        """
        async with self._session() as session:
            rows = await session.scalars(
                select(_PortabilityJobRow)
                .where(_PortabilityJobRow.tenant == tenant)
                .order_by(_PortabilityJobRow.created_at.desc(), _PortabilityJobRow.id.desc())
                .limit(limit)
            )
            return [PortabilityJob(row) for row in rows]

    async def expired(self, *, tenant: str, before: datetime) -> list[PortabilityJob]:
        """Jobs last touched before *before* — the sweep's candidates."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_PortabilityJobRow).where(
                    _PortabilityJobRow.tenant == tenant,
                    _PortabilityJobRow.updated_at < before,
                )
            )
            return [PortabilityJob(row) for row in rows]

    async def delete(self, *, tenant: str, job_id: str) -> None:
        """Drop one job row (its staged file is removed by the caller)."""
        async with self._session() as session:
            await session.execute(
                delete(_PortabilityJobRow).where(
                    _PortabilityJobRow.tenant == tenant,
                    _PortabilityJobRow.id == job_id,
                )
            )
            await session.commit()
