"""Postgres schema for the local calendar provider — tenant-scoped event store.

The ``calendar_events`` table is owned exclusively by this module.  It is
created lazily on startup (``LocalEventStore.init``).  Columns are prefixed
``calendar_`` to avoid collisions with other modules sharing the same Postgres
database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_calendar.models import Event


class _Base(DeclarativeBase):
    pass


class _StoredEvent(_Base):
    """ORM row for one calendar event in the local provider's store."""

    __tablename__ = "calendar_events"
    __table_args__ = (UniqueConstraint("tenant", "event_id", name="uq_calendar_tenant_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    event_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(512))
    start_dt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_dt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LocalEventStore:
    """CRUD helpers for the tenant-scoped local event store in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the ``calendar_events`` table if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def list_events(self, *, tenant: str, start: datetime, end: datetime) -> list[Event]:
        """Return all events for *tenant* that overlap ``[start, end)``."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredEvent)
                .where(
                    _StoredEvent.tenant == tenant,
                    _StoredEvent.start_dt < end,
                    _StoredEvent.end_dt > start,
                )
                .order_by(_StoredEvent.start_dt)
            )
            return [_row_to_event(row) for row in rows]

    async def create_event(
        self,
        *,
        tenant: str,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        location: str | None = None,
    ) -> Event:
        """Insert a new event and return the domain object."""
        event_id = str(uuid.uuid4())
        row = _StoredEvent(
            tenant=tenant,
            event_id=event_id,
            title=title,
            start_dt=start,
            end_dt=end,
            description=description,
            location=location,
        )
        async with self._session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _row_to_event(row)

    async def count(self, *, tenant: str) -> int:
        """Return the total number of stored events for *tenant*."""
        async with self._session() as session:
            result = await session.scalar(
                select(func.count(_StoredEvent.id)).where(_StoredEvent.tenant == tenant)
            )
            return int(result) if result is not None else 0

    async def delete_event(self, *, tenant: str, event_id: str) -> None:
        """Remove a single event by its ID."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredEvent).where(
                    _StoredEvent.tenant == tenant,
                    _StoredEvent.event_id == event_id,
                )
            )
            await session.commit()


def _row_to_event(row: _StoredEvent) -> Event:
    return Event(
        id=row.event_id,
        title=row.title,
        start=_ensure_utc(row.start_dt),
        end=_ensure_utc(row.end_dt),
        description=row.description,
        location=row.location,
        provider="local",
    )


def _ensure_utc(dt: datetime) -> datetime:
    """Attach UTC timezone to a naive datetime (SQLite returns naive values)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
