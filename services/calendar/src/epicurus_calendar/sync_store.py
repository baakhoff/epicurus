"""Tenant-scoped durable state for calendar's reconcile layer (#831, mirrors ADR-0096).

Three tables, all owned by this module and all scoped by ``tenant`` (constraint #1) even
though v1 is single-tenant:

- ``calendar_sync_state`` — per ``(tenant, account, collection)``: the provider's opaque
  incremental-sync cursor, the ``timeMin`` anchor the cursor inherited, and when it was last
  stamped. Its mere *existence* is what separates a **first-ever** sync (prime silently — a
  calendar you already had is not news) from a **resumed** one (a real gap to report).
- ``calendar_synced_event`` — the local cache of *what we last observed* per event, per
  collection. This is what turns a provider's "here is a changed event" into the three
  different things the spine wants to hear: an id we have never seen is a **creation**, an id
  whose :func:`~epicurus_calendar.spine.event_change_hash` moved is an **update** (and the
  cached start/end is what makes ``time_changed`` a real before/after comparison rather than a
  guess), an id that vanishes is a **cancellation** with a title still worth printing.
- ``calendar_self_writes`` — the self-write ledger. A short-lived marker per change this
  module made itself, so the reconcile can tell "the operator changed this in Google's UI"
  from "we changed this ten seconds ago and already said so".

The cursor is stored as an opaque string, never parsed: the store speaks no Google. A future
CalDAV backend fills the same column with its own ctag/sync-token and reuses this schema
unchanged — the same neutrality ADR-0096 holds for mail's cursor.

There is no migration framework; like every epicurus store these evolve via ``create_all`` +
the shared additive :func:`epicurus_core.db.ensure_columns` reconcile (ADR-0067). This is the
tables' first release, so the reconciled-column lists are empty — they exist so a *later*
column lands in an already-provisioned database instead of 500ing every read.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns


class _SyncBase(DeclarativeBase):
    pass


class _StoredSyncState(_SyncBase):
    """The incremental-sync cursor for one ``(tenant, account, collection)`` (#831)."""

    __tablename__ = "calendar_sync_state"
    __table_args__ = (
        UniqueConstraint("tenant", "account", "collection", name="uq_calendar_sync_state"),
    )

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    # The connected-account id (``google``); never ``local`` — the local store has no
    # "outside epicurus" to reconcile against, so it is never a sync target.
    account: Mapped[str] = mapped_column(String(64))
    # The collection (calendar) id within the account. The empty string means "the account's
    # own default calendar", exactly as ``CollectionRef.collection`` uses it.
    collection: Mapped[str] = mapped_column(String(255), default="")
    # The provider's opaque cursor (Google ``nextSyncToken``). NULL means "primed but without a
    # replayable cursor" — the next pass does a full sync and diffs, never a silent skip.
    sync_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The ``timeMin`` anchor the cursor inherited. A Google sync token replays changes only for
    # the window its initial full sync asked for, so the anchor has to be remembered: it is
    # what a later resync prunes aged-out cached rows against, so a row that merely fell out of
    # the window is never mistaken for a cancellation.
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _StoredSyncedEvent(_SyncBase):
    """One observed event — the cache the delta is classified against (#831)."""

    __tablename__ = "calendar_synced_event"
    __table_args__ = (
        UniqueConstraint(
            "tenant", "account", "collection", "event_id", name="uq_calendar_synced_event"
        ),
    )

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    account: Mapped[str] = mapped_column(String(64))
    collection: Mapped[str] = mapped_column(String(255), default="")
    # Provider event id. Google's expanded-instance ids are ``<series>_<original start>``, so
    # this is comfortably wider than the local store's bare-uuid column.
    event_id: Mapped[str] = mapped_column(String(255), index=True)
    # The series this event is an occurrence of, when it is one; NULL for a one-off. Kept
    # denormalised (rather than derived from the id) because a *tombstone* has no event object
    # left to read it from, and collapsing a series' occurrences into one emission needs it.
    series_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    start_dt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_dt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    # ``spine.event_change_hash`` of the last observed state — 12 hex chars.
    change_hash: Mapped[str] = mapped_column(String(32), default="")
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _StoredSelfWrite(_SyncBase):
    """A self-write marker: this module made this change, do not re-announce it (#831)."""

    __tablename__ = "calendar_self_writes"
    __table_args__ = (UniqueConstraint("tenant", "marker_key", name="uq_calendar_self_write"),)

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    # ``spine.self_write_key`` — ``"<event type>|<provider>:<id>"``.
    marker_key: Mapped[str] = mapped_column(String(512), index=True)
    # Nanosecond epoch (~1.8e18) — ``BigInteger``, never ``Integer``: SQLite tolerates the
    # int32 overflow so unit tests would pass and Postgres would then fail in production (the
    # knowledge-module mtime bug, and the same reason ``calendar_fired_markers`` uses it).
    expires_at_ns: Mapped[int] = mapped_column(BigInteger)


class SyncState(BaseModel):
    """The stored cursor for one collection (``None`` from the store means never synced)."""

    sync_token: str | None = None
    window_start: datetime | None = None
    synced_at: datetime | None = None


class SyncedEvent(BaseModel):
    """One cached observation of an event — what the next delta is classified against."""

    event_id: str
    series_id: str | None = None
    title: str = ""
    start: datetime
    end: datetime
    all_day: bool = False
    change_hash: str = ""


def _as_utc(value: datetime) -> datetime:
    """Tag a tz-naive value as UTC (SQLite round-trips drop the offset)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CalendarSyncStore:
    """Durable sync cursors + the observed-event cache, tenant- and collection-scoped."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_SyncBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        ensure_columns(sync_conn, _StoredSyncState.__table__, ())
        ensure_columns(sync_conn, _StoredSyncedEvent.__table__, ())
        ensure_columns(sync_conn, _StoredSelfWrite.__table__, ())

    # ── sync cursor ──────────────────────────────────────────────────────────

    async def get_state(self, *, tenant: str, account: str, collection: str) -> SyncState | None:
        """The stored cursor for this collection, or ``None`` when it was never synced.

        ``None`` is the load-bearing value: it is the *only* signal that separates a
        first-ever sync (prime the cache in silence) from a resumed one (report the gap).
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredSyncState).where(
                    _StoredSyncState.tenant == tenant,
                    _StoredSyncState.account == account,
                    _StoredSyncState.collection == collection,
                )
            )
            if row is None:
                return None
            return SyncState(
                sync_token=row.sync_token,
                window_start=_as_utc(row.window_start) if row.window_start else None,
                synced_at=_as_utc(row.synced_at) if row.synced_at else None,
            )

    async def set_state(
        self,
        *,
        tenant: str,
        account: str,
        collection: str,
        sync_token: str | None,
        window_start: datetime | None,
    ) -> None:
        """Persist the advanced cursor (upsert, delete-then-insert)."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredSyncState).where(
                    _StoredSyncState.tenant == tenant,
                    _StoredSyncState.account == account,
                    _StoredSyncState.collection == collection,
                )
            )
            session.add(
                _StoredSyncState(
                    tenant=tenant,
                    account=account,
                    collection=collection,
                    sync_token=sync_token,
                    window_start=window_start,
                    synced_at=datetime.now(UTC),
                )
            )
            await session.commit()

    # ── observed-event cache ─────────────────────────────────────────────────

    async def get_events(
        self, *, tenant: str, account: str, collection: str
    ) -> dict[str, SyncedEvent]:
        """Every cached observation for this collection, keyed by event id."""
        async with self._session() as session:
            rows = (
                await session.scalars(
                    select(_StoredSyncedEvent).where(
                        _StoredSyncedEvent.tenant == tenant,
                        _StoredSyncedEvent.account == account,
                        _StoredSyncedEvent.collection == collection,
                    )
                )
            ).all()
        return {row.event_id: _to_synced(row) for row in rows}

    async def get_event(
        self, *, tenant: str, account: str, collection: str, event_id: str
    ) -> SyncedEvent | None:
        """One cached observation, or ``None`` if this collection has never reported it."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredSyncedEvent).where(
                    _StoredSyncedEvent.tenant == tenant,
                    _StoredSyncedEvent.account == account,
                    _StoredSyncedEvent.collection == collection,
                    _StoredSyncedEvent.event_id == event_id,
                )
            )
            return _to_synced(row) if row is not None else None

    async def upsert_events(
        self, *, tenant: str, account: str, collection: str, events: Sequence[SyncedEvent]
    ) -> None:
        """Record the current observation of each event (delete-then-insert per id)."""
        if not events:
            return
        async with self._session() as session:
            await session.execute(
                delete(_StoredSyncedEvent).where(
                    _StoredSyncedEvent.tenant == tenant,
                    _StoredSyncedEvent.account == account,
                    _StoredSyncedEvent.collection == collection,
                    _StoredSyncedEvent.event_id.in_([e.event_id for e in events]),
                )
            )
            now = datetime.now(UTC)
            for event in events:
                session.add(
                    _StoredSyncedEvent(
                        tenant=tenant,
                        account=account,
                        collection=collection,
                        event_id=event.event_id,
                        series_id=event.series_id,
                        title=event.title[:512],
                        start_dt=event.start,
                        end_dt=event.end,
                        all_day=event.all_day,
                        change_hash=event.change_hash,
                        seen_at=now,
                    )
                )
            await session.commit()

    async def remove_events(
        self, *, tenant: str, account: str, collection: str, event_ids: Iterable[str]
    ) -> None:
        """Drop cached observations (a cancellation, or a row that aged out of the window)."""
        ids = list(event_ids)
        if not ids:
            return
        async with self._session() as session:
            await session.execute(
                delete(_StoredSyncedEvent).where(
                    _StoredSyncedEvent.tenant == tenant,
                    _StoredSyncedEvent.account == account,
                    _StoredSyncedEvent.collection == collection,
                    _StoredSyncedEvent.event_id.in_(ids),
                )
            )
            await session.commit()

    async def replace_events(
        self, *, tenant: str, account: str, collection: str, events: Sequence[SyncedEvent]
    ) -> None:
        """Replace this collection's whole cache — the priming path of a first-ever sync."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredSyncedEvent).where(
                    _StoredSyncedEvent.tenant == tenant,
                    _StoredSyncedEvent.account == account,
                    _StoredSyncedEvent.collection == collection,
                )
            )
            await session.commit()
        await self.upsert_events(
            tenant=tenant, account=account, collection=collection, events=events
        )

    async def count_events(self, *, tenant: str) -> int:
        """How many observations this tenant's cache holds (status/diagnostics)."""
        async with self._session() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(_StoredSyncedEvent)
                .where(_StoredSyncedEvent.tenant == tenant)
            )
            return int(total or 0)


def _to_synced(row: _StoredSyncedEvent) -> SyncedEvent:
    return SyncedEvent(
        event_id=row.event_id,
        series_id=row.series_id,
        title=row.title,
        start=_as_utc(row.start_dt),
        end=_as_utc(row.end_dt),
        all_day=row.all_day,
        change_hash=row.change_hash,
    )


class SelfWriteLedger:
    """Short-lived markers for changes *this module* made, so the reconcile stays quiet (#831).

    Durable rather than in-memory on purpose: a write can land seconds before a restart, and
    the reconcile that then notices it must still know the change was already announced.

    Two read shapes, and the difference matters:

    - :meth:`consume` — the exact-id match, and *only* a genuine single-occurrence one. It
      removes the marker, so a *second*, genuinely external change to the same event moments
      later is announced normally.
    - :meth:`peek` — the series match. One series-wide write comes back as one change per
      occurrence, and (with the reconcile collapsing those into a single emission) the marker
      still has to survive an occurrence arriving in a later pass, so this leaves it in place
      to expire on its own.

    A series marker is therefore released by :meth:`prune`/TTL alone, never by a read — there is
    no point at which a series write is known to be "fully observed" (#843). The caller decides
    which shape applies from how it derived the id it is asking about, never by comparing the
    id to the series id: the reconcile's collapse makes those equal, and reading the equality
    as "an exact match" is what sent a series marker down :meth:`consume` and re-announced a
    straddling series write.
    """

    def __init__(self, engine: AsyncEngine, *, ttl_s: float = 900.0) -> None:
        self._engine = engine
        self._ttl_s = ttl_s
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema (shared metadata with :class:`CalendarSyncStore`; idempotent)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_SyncBase.metadata.create_all)
            await conn.run_sync(CalendarSyncStore._ensure_columns)

    async def record(self, *, tenant: str, keys: Iterable[str]) -> None:
        """Mark each key as "written by us", expiring after the configured TTL.

        Best-effort by contract: the caller records *after* the provider write succeeded and
        *before* it emits, and a failure here must never fail a write that already landed —
        it costs at most one duplicate event, never a lost one.
        """
        wanted = [key for key in keys if key]
        if not wanted:
            return
        expires_at_ns = time.time_ns() + int(self._ttl_s * 1_000_000_000)
        async with self._session() as session:
            await session.execute(
                delete(_StoredSelfWrite).where(
                    _StoredSelfWrite.tenant == tenant,
                    _StoredSelfWrite.marker_key.in_(wanted),
                )
            )
            for key in wanted:
                session.add(
                    _StoredSelfWrite(tenant=tenant, marker_key=key, expires_at_ns=expires_at_ns)
                )
            await session.commit()

    async def consume(self, *, tenant: str, key: str) -> bool:
        """``True`` if *key* was marked (and remove it); ``False`` otherwise."""
        now_ns = time.time_ns()
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredSelfWrite).where(
                    _StoredSelfWrite.tenant == tenant,
                    _StoredSelfWrite.marker_key == key,
                )
            )
            if row is None:
                return False
            fresh = row.expires_at_ns > now_ns
            await session.delete(row)
            await session.commit()
            return fresh

    async def peek(self, *, tenant: str, key: str) -> bool:
        """``True`` if *key* is marked and unexpired, leaving the marker in place."""
        now_ns = time.time_ns()
        async with self._session() as session:
            expires = await session.scalar(
                select(_StoredSelfWrite.expires_at_ns).where(
                    _StoredSelfWrite.tenant == tenant,
                    _StoredSelfWrite.marker_key == key,
                )
            )
            return expires is not None and expires > now_ns

    async def prune(self, *, tenant: str) -> int:
        """Drop expired markers; returns how many went. Called once per reconcile pass."""
        now_ns = time.time_ns()
        async with self._session() as session:
            result = await session.execute(
                delete(_StoredSelfWrite).where(
                    _StoredSelfWrite.tenant == tenant,
                    _StoredSelfWrite.expires_at_ns <= now_ns,
                )
            )
            await session.commit()
            return int(cast("CursorResult[Any]", result).rowcount or 0)
