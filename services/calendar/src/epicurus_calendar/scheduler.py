"""Lead-time scheduler: `calendar.event_starting_soon` / `calendar.event_ended` (#664).

Calendar has no periodic background job today — this is the first one. A poll loop (mirroring
core-app's `MaintenanceOrchestrator.run_periodic`, ADR-0098 §3: a poll, not a single computed
sleep, so a mid-run lead-time change takes effect on the next tick without a restart) checks,
each tick, which upcoming events are inside their lead window and which recently-ended events
haven't been reported yet — firing each at most once via a durable marker table that survives a
restart.

No-firehose note (#664): the first tick after a fresh start could "discover" every event already
inside its lead window and fire for each. That is accepted as correct, not a bug — unlike mail's
no-firehose rule (which suppresses an entire *initial sync*, because "every message you've ever
received" is not news), there is no equivalent backlog concept here: an event that is validly
"starting soon" right now deserves exactly one notification, whether the process has been up for
a week or ten seconds. The fire-once marker still guarantees it is exactly one, not zero and not
repeated.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, String, UniqueConstraint, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_calendar.lead_time_prefs import LeadTimePrefsStore
from epicurus_calendar.models import DateTimeRange, Event
from epicurus_calendar.providers.base import CalendarProvider
from epicurus_core import EntityRef, EventBus, emit_event, get_logger
from epicurus_core.db import ensure_columns

log = get_logger("epicurus_calendar.scheduler")

EVENT_STARTING_SOON = "calendar.event_starting_soon"
EVENT_ENDED = "calendar.event_ended"

_MARKER_STARTING_SOON = "starting_soon"
_MARKER_ENDED = "ended"

DEFAULT_POLL_INTERVAL_S = 60.0
"""How often the scheduler ticks. Short enough that a 15-minute default lead is honored within
about a minute of accuracy; long enough not to hammer the provider every few seconds."""

DEFAULT_LOOKBACK_MINUTES = 60
"""How far past `now` a tick still checks for a just-ended event. Bounded, not unlimited
backfill: an event that ended more than this long before the scheduler was last able to tick
(e.g. the module was down) is never reported as `event_ended` — accepted, not fixed, in this
PR; the fire-once *marker* is what's proven to survive a restart, not full historical replay."""


class _MarkerBase(DeclarativeBase):
    pass


class _FiredMarkerRow(_MarkerBase):
    """One fire-once marker: this ``(tenant, event, marker)`` has already been emitted (#664).

    ``fired_at_ns`` is a nanosecond epoch (~1.8e18) — ``BigInteger``, never ``Integer`` (the
    knowledge-module mtime bug: SQLite tolerates the int32 overflow so unit tests pass, then
    Postgres doesn't).
    """

    __tablename__ = "calendar_fired_markers"
    __table_args__ = (
        UniqueConstraint("tenant", "event_id", "marker", name="uq_calendar_fired_marker"),
    )

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    event_id: Mapped[str] = mapped_column(String(255), index=True)
    marker: Mapped[str] = mapped_column(String(32))
    fired_at_ns: Mapped[int] = mapped_column(BigInteger)


class FiredMarkerStore:
    """Durable fire-once markers for the lead-time scheduler (#664)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_MarkerBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        ensure_columns(sync_conn, _FiredMarkerRow.__table__, ())

    async def try_claim(self, *, tenant: str, event_id: str, marker: str) -> bool:
        """Atomically claim ``(tenant, event_id, marker)`` — ``True`` if this call won.

        A database constraint decides, not a read-then-write check that races (the same
        posture the event spine's own dedup takes, ADR-0103): two ticks racing the same
        marker both attempt the insert; the loser's violates the unique constraint and is
        treated as "already fired," not an error.
        """
        async with self._session() as session:
            session.add(
                _FiredMarkerRow(
                    tenant=tenant, event_id=event_id, marker=marker, fired_at_ns=time.time_ns()
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def has_fired(self, *, tenant: str, event_id: str, marker: str) -> bool:
        """Whether ``(tenant, event_id, marker)`` has already been claimed — read-only,
        for tests."""
        async with self._session() as session:
            row = await session.scalar(
                select(_FiredMarkerRow.pk).where(
                    _FiredMarkerRow.tenant == tenant,
                    _FiredMarkerRow.event_id == event_id,
                    _FiredMarkerRow.marker == marker,
                )
            )
            return row is not None


def _marker_key(event: Event) -> str:
    """A fire-once marker's event identity: provider-qualified, like the emitted ``dedup_key``."""
    return f"{event.provider}:{event.id}"


async def _fire_once(
    *,
    markers: FiredMarkerStore,
    bus: EventBus,
    tenant: str,
    event: Event,
    event_type: str,
    marker: str,
    extra_payload: dict[str, object] | None = None,
) -> None:
    key = _marker_key(event)
    if not await markers.try_claim(tenant=tenant, event_id=key, marker=marker):
        return
    payload: dict[str, object] = {
        "title": event.title[:200],
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
    }
    if extra_payload:
        payload.update(extra_payload)
    try:
        await emit_event(
            bus,
            tenant_id=tenant,
            module="calendar",
            event_type=event_type,
            dedup_key=f"{key}:{marker}",
            payload=payload,
            entity_ref=EntityRef(
                ref_id=event.id, module="calendar", kind="event", title=event.title
            ),
        )
    except Exception as exc:  # a spine hiccup must never crash the scheduler tick
        log.warning(f"{event_type} emit failed", event_id=event.id, error=str(exc))


async def tick(
    *,
    tenant: str,
    provider: CalendarProvider,
    lead_prefs: LeadTimePrefsStore,
    markers: FiredMarkerStore,
    bus: EventBus,
    now: datetime,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
) -> None:
    """One scheduler pass: fire `event_starting_soon` / `event_ended` for anything newly due.

    Lead time is minutes, so honoring it is pure instant comparison — no timezone resolution
    needed (contrast tasks' day-granular `task_due_soon`, ADR-0039). One `list_events` call
    covers both directions (events ending as far back as *lookback_minutes*, starting as far
    ahead as the lead window) rather than two provider round trips per tick.
    """
    lead_minutes = await lead_prefs.get_lead_minutes(tenant)
    window_end = now + timedelta(minutes=lead_minutes)
    window_start = now - timedelta(minutes=lookback_minutes)
    events = await provider.list_events(
        tenant_id=tenant, time_range=DateTimeRange(start=window_start, end=window_end)
    )
    for event in events:
        if now <= event.start <= window_end:
            await _fire_once(
                markers=markers,
                bus=bus,
                tenant=tenant,
                event=event,
                event_type=EVENT_STARTING_SOON,
                marker=_MARKER_STARTING_SOON,
                extra_payload={"lead_minutes": lead_minutes},
            )
        if event.end <= now:
            await _fire_once(
                markers=markers,
                bus=bus,
                tenant=tenant,
                event=event,
                event_type=EVENT_ENDED,
                marker=_MARKER_ENDED,
            )


async def run_periodic(
    *,
    tenant: str,
    provider: CalendarProvider,
    lead_prefs: LeadTimePrefsStore,
    markers: FiredMarkerStore,
    bus: EventBus,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Poll forever, ticking first and sleeping after — so a fresh restart checks promptly
    rather than waiting a full interval before its first pass. One bad tick (a provider hiccup,
    a transient DB error) is logged and skipped, never kills the loop."""
    while True:
        try:
            await tick(
                tenant=tenant,
                provider=provider,
                lead_prefs=lead_prefs,
                markers=markers,
                bus=bus,
                now=datetime.now(UTC),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("calendar lead-time scheduler tick failed", error=str(exc))
        await asyncio.sleep(poll_interval_s)
