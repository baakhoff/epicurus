"""Postgres schema for the local calendar provider — tenant-scoped event store.

The ``calendar_events`` table is owned exclusively by this module.  It is
created lazily on startup (``LocalEventStore.init``).  Columns are prefixed
``calendar_`` to avoid collisions with other modules sharing the same Postgres
database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    CursorResult,
    DateTime,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_calendar.models import Event
from epicurus_core.db import ensure_columns

# Columns added after the table's first release; reconciled in place at startup by
# ``LocalEventStore._ensure_columns`` (the store has no migration framework).
_ADDED_COLUMNS = ("all_day",)


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
    # All-day (date-only) event — ``start_dt``/``end_dt`` are UTC-midnight day boundaries
    # with ``end_dt`` exclusive (see ``Event.all_day``). Added after first release, so it
    # is reconciled in place by ``_ensure_columns``; existing rows read NULL → False.
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LocalEventStore:
    """CRUD helpers for the tenant-scoped local event store in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the ``calendar_events`` table, then add any later-added columns."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Reconcile columns added after first release via the shared additive helper (#249).

        ``all_day`` postdates the table's first release; a database provisioned before then
        lacks it and every local event read 500s on Postgres until it is added in place. It
        has no server default, so it is added nullable and existing rows read NULL, coerced
        to ``False`` in ``_row_to_event``. See :func:`epicurus_core.db.ensure_columns`.
        """
        ensure_columns(sync_conn, _StoredEvent.__table__, _ADDED_COLUMNS)

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

    async def get_event(self, *, tenant: str, event_id: str) -> Event | None:
        """Return the single event with *event_id* for *tenant*, or ``None``."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredEvent).where(
                    _StoredEvent.tenant == tenant,
                    _StoredEvent.event_id == event_id,
                )
            )
            return _row_to_event(row) if row is not None else None

    async def create_event(
        self,
        *,
        tenant: str,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        location: str | None = None,
        all_day: bool = False,
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
            all_day=all_day,
        )
        async with self._session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _row_to_event(row)

    async def update_event(
        self,
        *,
        tenant: str,
        event_id: str,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        all_day: bool | None = None,
    ) -> Event | None:
        """Apply non-``None`` fields to an event; return it, or ``None`` if absent.

        A partial edit: only the fields the caller supplies are changed. Returns
        ``None`` when no such event exists for *tenant* (#208).
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredEvent).where(
                    _StoredEvent.tenant == tenant,
                    _StoredEvent.event_id == event_id,
                )
            )
            if row is None:
                return None
            if title is not None:
                row.title = title
            if start is not None:
                row.start_dt = start
            if end is not None:
                row.end_dt = end
            if description is not None:
                row.description = description
            if location is not None:
                row.location = location
            if all_day is not None:
                row.all_day = all_day
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

    async def delete_event(self, *, tenant: str, event_id: str) -> bool:
        """Remove a single event by its ID; return ``True`` if a row was deleted (#208)."""
        async with self._session() as session:
            result = await session.execute(
                delete(_StoredEvent).where(
                    _StoredEvent.tenant == tenant,
                    _StoredEvent.event_id == event_id,
                )
            )
            await session.commit()
            return (cast("CursorResult[Any]", result).rowcount or 0) > 0


def _row_to_event(row: _StoredEvent) -> Event:
    return Event(
        id=row.event_id,
        title=row.title,
        start=_ensure_utc(row.start_dt),
        end=_ensure_utc(row.end_dt),
        description=row.description,
        location=row.location,
        provider="local",
        # Rows written before the column existed read NULL → False.
        all_day=bool(row.all_day),
    )


def _ensure_utc(dt: datetime) -> datetime:
    """Attach UTC timezone to a naive datetime (SQLite returns naive values)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
