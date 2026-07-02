"""Postgres schema for the local calendar provider — tenant-scoped event store.

The ``calendar_events`` table is owned exclusively by this module.  It is
created lazily on startup (``LocalEventStore.init``).  Columns are prefixed
``calendar_`` to avoid collisions with other modules sharing the same Postgres
database.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple, cast

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

from epicurus_calendar.models import Attendee, Event
from epicurus_core.db import ensure_columns

# Columns added after the table's first release; reconciled in place at startup by
# ``LocalEventStore._ensure_columns`` (the store has no migration framework).
_ADDED_COLUMNS = ("all_day", "recurrence", "recurring_event_id", "excluded", "attendees")

# Separates a recurring series' event id from an occurrence's original-start suffix in an
# instance id, e.g. ``<series-uuid>_20260710T150000Z`` — the same convention Google's own
# expanded-instance ids use, so local and Google ids read consistently (#432). A plain
# event id is a bare uuid4 (hyphens only), which never contains ``_``, so this is unambiguous.
_INSTANCE_SEP = "_"
_INSTANCE_TS_FORMAT = "%Y%m%dT%H%M%SZ"


def instance_id(series_id: str, original_start: datetime) -> str:
    """The stable id of one occurrence of a recurring series (#432)."""
    ts = original_start.astimezone(UTC).strftime(_INSTANCE_TS_FORMAT)
    return f"{series_id}{_INSTANCE_SEP}{ts}"


def parse_instance_id(event_id: str) -> tuple[str, datetime] | None:
    """Split an instance id into ``(series_id, original_start)``, or ``None`` if not one."""
    series_id, sep, suffix = event_id.rpartition(_INSTANCE_SEP)
    if not sep:
        return None
    try:
        original_start = datetime.strptime(suffix, _INSTANCE_TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return series_id, original_start


def _dump_attendees(attendees: list[Attendee] | None) -> str | None:
    """JSON-encode attendees for storage; ``None`` means "leave the column untouched"."""
    if attendees is None:
        return None
    return json.dumps([a.model_dump(mode="json") for a in attendees])


def _load_attendees(raw: str | None) -> list[Attendee]:
    """Decode the stored attendees column; a missing/blank value is no guests."""
    if not raw:
        return []
    return [Attendee.model_validate(a) for a in json.loads(raw)]


class _Base(DeclarativeBase):
    pass


class _StoredEvent(_Base):
    """ORM row for one calendar event in the local provider's store.

    A row is one of three kinds (#432): a **plain** event (``recurrence`` and
    ``recurring_event_id`` both ``NULL``); a recurring **series master**
    (``recurrence`` set, ``recurring_event_id`` ``NULL`` — its own ``start_dt`` is the
    first occurrence); or an **exception** to a series (``recurring_event_id`` names the
    master; its ``event_id`` encodes the *original* occurrence start it overrides via
    :func:`instance_id` — see :func:`parse_instance_id`). ``excluded`` tombstones a single
    deleted occurrence without touching the rest of the series.
    """

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
    # RFC 5545 RRULE string (no ``"RRULE:"`` prefix) on a series master; NULL otherwise (#432).
    recurrence: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The master's event_id, on an exception row only; NULL for a plain event or a master
    # itself. Indexed — every exception lookup for a series filters on this (#432).
    recurring_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Tombstones a single occurrence (an exception row with excluded=True is a deleted
    # instance, never returned) — meaningless outside an exception row (#432).
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    # JSON-encoded list of attendee dicts (see Attendee); NULL/blank means no guests (#432).
    attendees: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExceptionRow(NamedTuple):
    """One exception row for a recurring series — the original slot it overrides plus the
    resulting event (built even for a tombstone; callers must check ``excluded`` first)."""

    original_start: datetime
    excluded: bool
    event: Event


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
        """Return **plain** (non-recurring) events for *tenant* overlapping ``[start, end)``.

        Excludes series masters and exceptions (#432) — those are expanded separately by
        :meth:`list_master_events` + :meth:`list_exceptions`, which the provider combines
        with this query's results; a master's own ``start_dt`` is only its first occurrence
        and an exception's raw row is meaningless outside its series' schedule, so neither
        belongs in a bare overlap scan.
        """
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredEvent)
                .where(
                    _StoredEvent.tenant == tenant,
                    _StoredEvent.start_dt < end,
                    _StoredEvent.end_dt > start,
                    _StoredEvent.recurrence.is_(None),
                    _StoredEvent.recurring_event_id.is_(None),
                )
                .order_by(_StoredEvent.start_dt)
            )
            return [_row_to_event(row) for row in rows]

    async def list_master_events(
        self, *, tenant: str, start: datetime, end: datetime
    ) -> list[Event]:
        """Recurring series masters that *might* occur in ``[start, end)`` (#432).

        A coarse pre-filter (``start_dt <= end`` — a master's own start is its first
        occurrence, so one created long ago can still recur into a future window); the
        caller expands each master's RRULE and discards any with no occurrence actually in
        range. A series whose rule has already ended (``COUNT``/``UNTIL`` exhausted) is
        fetched here but yields zero occurrences on expansion — a harmless, bounded cost.
        """
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredEvent).where(
                    _StoredEvent.tenant == tenant,
                    _StoredEvent.recurrence.is_not(None),
                    _StoredEvent.start_dt < end,
                )
            )
            return [_row_to_event(row) for row in rows]

    async def list_exceptions(self, *, tenant: str, series_id: str) -> list[ExceptionRow]:
        """Every exception row (edited or tombstoned occurrence) for one series (#432)."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredEvent).where(
                    _StoredEvent.tenant == tenant,
                    _StoredEvent.recurring_event_id == series_id,
                )
            )
            out: list[ExceptionRow] = []
            for row in rows:
                parsed = parse_instance_id(row.event_id)
                if parsed is None:
                    continue  # a corrupt/foreign event_id — skip rather than crash the series
                _series, original_start = parsed
                out.append(ExceptionRow(original_start, row.excluded, _row_to_event(row)))
            return out

    async def upsert_exception(
        self,
        *,
        tenant: str,
        series_id: str,
        original_start: datetime,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None,
        location: str | None,
        all_day: bool,
        excluded: bool,
        attendees: list[Attendee] | None = None,
    ) -> Event:
        """Create or replace the exception overriding one occurrence of *series_id* (#432).

        Always a full replace (every field given), matching the one call site
        (:class:`~epicurus_calendar.providers.local.LocalCalendarProvider`'s ``edit_scope=
        "this"`` path, which resolves the merged fields itself before calling this).
        """
        event_id = instance_id(series_id, original_start)
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredEvent).where(
                    _StoredEvent.tenant == tenant, _StoredEvent.event_id == event_id
                )
            )
            if row is None:
                row = _StoredEvent(tenant=tenant, event_id=event_id, recurring_event_id=series_id)
                session.add(row)
            row.title = title
            row.start_dt = _to_utc(start)
            row.end_dt = _to_utc(end)
            row.description = description
            row.location = location
            row.all_day = all_day
            row.excluded = excluded
            row.attendees = _dump_attendees(attendees)
            await session.commit()
            await session.refresh(row)
            return _row_to_event(row)

    async def delete_exceptions_for(self, *, tenant: str, series_id: str) -> None:
        """Remove every exception row of a series — called when the series itself is deleted."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredEvent).where(
                    _StoredEvent.tenant == tenant, _StoredEvent.recurring_event_id == series_id
                )
            )
            await session.commit()

    async def get_event(self, *, tenant: str, event_id: str) -> Event | None:
        """Return the single **stored** row with *event_id* for *tenant*, or ``None``.

        A row-level lookup only — an unmodified recurring instance has no row of its own
        (it is synthesized on read); the provider layer handles that case.
        """
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
        recurrence: str | None = None,
        attendees: list[Attendee] | None = None,
    ) -> Event:
        """Insert a new event (optionally a recurring series master) and return it (#432)."""
        event_id = str(uuid.uuid4())
        row = _StoredEvent(
            tenant=tenant,
            event_id=event_id,
            title=title,
            start_dt=_to_utc(start),
            end_dt=_to_utc(end),
            description=description,
            location=location,
            all_day=all_day,
            recurrence=recurrence,
            attendees=_dump_attendees(attendees),
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
        recurrence: str | None = None,
        attendees: list[Attendee] | None = None,
    ) -> Event | None:
        """Apply non-``None`` fields to an event; return it, or ``None`` if absent.

        A partial edit: only the fields the caller supplies are changed. Returns
        ``None`` when no such event exists for *tenant* (#208). Always acts on the row
        named by *event_id* directly (a plain event or a series master) — recurring
        instance/scope resolution is the provider's job, not this store's (#432).
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
                row.start_dt = _to_utc(start)
            if end is not None:
                row.end_dt = _to_utc(end)
            if description is not None:
                row.description = description
            if location is not None:
                row.location = location
            if all_day is not None:
                row.all_day = all_day
            if recurrence is not None:
                row.recurrence = recurrence
            if attendees is not None:
                row.attendees = _dump_attendees(attendees)
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
        """Remove a single event row by its ID; return ``True`` if a row was deleted (#208)."""
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
    # An exception row's original_start is encoded in its own event_id (#432) — recovered
    # here so every reader (list_exceptions included, via this same function) sees it
    # without a redundant column to keep in sync.
    original_start = None
    if row.recurring_event_id is not None:
        parsed = parse_instance_id(row.event_id)
        original_start = parsed[1] if parsed is not None else None
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
        recurrence=row.recurrence,
        recurring_event_id=row.recurring_event_id,
        original_start=original_start,
        attendees=_load_attendees(row.attendees),
    )


def _ensure_utc(dt: datetime) -> datetime:
    """Attach UTC timezone to a naive datetime (SQLite returns naive values)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _to_utc(dt: datetime) -> datetime:
    """Normalize an instant to UTC before storage.

    SQLAlchemy's SQLite ``DateTime`` silently drops a non-UTC offset (keeping the wall
    time), so a ``+02:00`` instant written as-is reads back shifted; Postgres
    ``timestamptz`` is unaffected. Converting up front keeps both backends exact now
    that tool inputs can carry any offset (#433).
    """
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
