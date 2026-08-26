"""One operator action, one spine event — the self-write suppression contract (#831).

Calendar now has two emitters: the provider-write seam (immediate, on every write made
*through* this module) and the reconcile loop (minutes later, on everything the provider
reports). Left alone they would both announce every write the operator makes here, which is
worse than the hole #831 set out to close — a duplicate is indistinguishable from a second
edit to whatever is downstream.

These tests wire both emitters over one fake Google backend that really records the writes and
really replays them through its change feed, and assert the total emission count end to end:
**exactly one**, from the write seam, with the reconcile staying quiet — and, crucially, the
inverse: a change the module did *not* make is still announced, including a second edit to the
same event after a marker has done its job.

File-backed SQLite under ``tmp_path`` throughout (never in-memory + ``StaticPool``), disposed in
teardown.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_calendar.db import LocalEventStore
from epicurus_calendar.models import Attendee, DateTimeRange, Event
from epicurus_calendar.providers.base import (
    CalendarProvider,
    EditScope,
    EventChange,
    EventSyncPage,
)
from epicurus_calendar.providers.local import LocalCalendarProvider
from epicurus_calendar.providers.router import CollectionRouter
from epicurus_calendar.sync import CalendarReconciler
from epicurus_calendar.sync_store import CalendarSyncStore, SelfWriteLedger
from epicurus_core import Collection, CollectionPrefs, CollectionRef, EventEnvelope

TENANT = "local"
GOOGLE = CollectionRef(account="google", collection="primary")


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[dict[str, object]] = []

    async def publish(self, subject: str, data: object, tenant_id: str | None = None) -> None:
        assert isinstance(data, dict)
        self.published.append(data)

    def envelopes(self) -> list[EventEnvelope]:
        return [EventEnvelope.model_validate(data) for data in self.published]

    def types(self) -> list[str]:
        return [envelope.type for envelope in self.envelopes()]


class _Prefs:
    def __init__(self, prefs: CollectionPrefs) -> None:
        self._prefs = prefs

    async def get_collections(self) -> CollectionPrefs:
        return self._prefs


class _FakeGoogle(CalendarProvider):
    """A Google stand-in that actually stores writes **and** replays them through a feed.

    That double duty is the point: a suppression test that emitted the change feed by hand
    could quietly disagree with what the write path produced, and prove nothing.
    """

    name = "google"
    supports_sync = True

    def __init__(self, *, available: bool = True) -> None:
        self.events: dict[str, Event] = {}
        self.pending: list[EventChange] = []
        self.available = available

    # ── writes (also feed the change log) ────────────────────────────────────

    async def create_event(
        self,
        *,
        tenant_id: str,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
        recurrence: str | None = None,
        attendees: list[Attendee] | None = None,
        recurrence_timezone: str | None = None,
        add_meet: bool = False,
    ) -> Event:
        event_id = uuid.uuid4().hex[:12]
        event = Event(
            id=event_id,
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            provider="google",
            all_day=all_day,
            recurrence=recurrence,
        )
        self.events[event_id] = event
        if recurrence:
            # Google's feed reports a series as one expanded occurrence per instance.
            for week in range(3):
                self.arrives(
                    Event(
                        id=f"{event_id}_{week}",
                        title=title,
                        start=start + timedelta(days=7 * week),
                        end=end + timedelta(days=7 * week),
                        provider="google",
                        recurring_event_id=event_id,
                    )
                )
        else:
            self.arrives(event)
        return event

    async def update_event(
        self,
        *,
        tenant_id: str,
        event_id: str,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool | None = None,
        recurrence: str | None = None,
        attendees: list[Attendee] | None = None,
        recurrence_timezone: str | None = None,
        edit_scope: EditScope = "this",
    ) -> Event | None:
        current = self.events.get(event_id)
        if current is None:
            return None
        updated = current.model_copy(
            update={
                key: value
                for key, value in {
                    "title": title,
                    "start": start,
                    "end": end,
                    "location": location,
                }.items()
                if value is not None
            }
        )
        self.events[event_id] = updated
        self.arrives(updated)
        return updated

    async def delete_event(
        self,
        *,
        tenant_id: str,
        event_id: str,
        calendar_id: str | None = None,
        edit_scope: EditScope = "this",
    ) -> bool:
        if event_id not in self.events:
            return False
        del self.events[event_id]
        self.pending.append(EventChange(event_id=event_id, cancelled=True))
        return True

    # ── the change feed ──────────────────────────────────────────────────────

    def arrives(self, event: Event) -> None:
        """Record a live change — used by the writes above *and* by tests simulating the
        operator editing something in Google's own UI."""
        self.events.setdefault(event.id, event)
        self.events[event.id] = event
        self.pending.append(
            EventChange(
                event_id=event.id,
                event=event,
                series_id=event.recurring_event_id,
            )
        )

    def removed(self, event_id: str) -> None:
        self.events.pop(event_id, None)
        self.pending.append(EventChange(event_id=event_id, cancelled=True))

    async def full_sync(
        self, *, tenant_id: str, calendar_id: str | None = None, since: datetime
    ) -> EventSyncPage:
        self.pending.clear()
        live = [e for e in self.events.values() if e.recurrence is None]
        return EventSyncPage(
            changes=[
                EventChange(event_id=e.id, event=e, series_id=e.recurring_event_id) for e in live
            ],
            next_cursor="tok-1",
        )

    async def changed_events_since(
        self, *, tenant_id: str, calendar_id: str | None = None, cursor: str
    ) -> EventSyncPage | None:
        changes = list(self.pending)
        self.pending.clear()
        return EventSyncPage(changes=changes, next_cursor="tok-next")

    # ── reads ────────────────────────────────────────────────────────────────

    async def get_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> Event | None:
        return self.events.get(event_id)

    async def list_events(
        self, *, tenant_id: str, time_range: DateTimeRange, calendar_id: str | None = None
    ) -> list[Event]:
        return list(self.events.values())

    async def find_free_slots(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> list[DateTimeRange]:
        return []

    async def is_available(self, *, tenant_id: str) -> bool:
        return self.available

    async def list_collections(self, *, tenant_id: str) -> list[Collection]:
        return [Collection(account="google", collection="primary", title="Primary")]


class _Wiring:
    """Both emitters over one backend, sharing one ledger — the shape ``app.py`` builds."""

    def __init__(
        self,
        *,
        router: CollectionRouter,
        reconciler: CalendarReconciler,
        google: _FakeGoogle,
        bus: _RecordingBus,
        ledger: SelfWriteLedger,
    ) -> None:
        self.router = router
        self.reconciler = reconciler
        self.google = google
        self.bus = bus
        self.ledger = ledger


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'calendar.db'}")
    yield engine
    await engine.dispose()


async def _wire(
    engine: AsyncEngine, *, google: _FakeGoogle | None = None, prefs: CollectionPrefs | None = None
) -> _Wiring:
    local_store = LocalEventStore(engine)
    await local_store.init()
    sync_store = CalendarSyncStore(engine)
    await sync_store.init()
    ledger = SelfWriteLedger(engine, ttl_s=900.0)
    await ledger.init()
    backend = google if google is not None else _FakeGoogle()
    bus = _RecordingBus()
    router = CollectionRouter(
        local=LocalCalendarProvider(store=local_store),
        external={"google": backend},
        prefs=_Prefs(prefs or CollectionPrefs(enabled=[GOOGLE], active=GOOGLE)),
        bus=bus,  # type: ignore[arg-type]
        ledger=ledger,
    )
    reconciler = CalendarReconciler(
        targets=router,
        store=sync_store,
        ledger=ledger,
        bus=bus,  # type: ignore[arg-type]
        tenant_id=TENANT,
    )
    await reconciler.reconcile()  # prime, so later passes take the incremental path
    return _Wiring(router=router, reconciler=reconciler, google=backend, bus=bus, ledger=ledger)


def _dt(hour: int) -> datetime:
    return datetime.now(UTC).replace(microsecond=0, second=0, minute=0, hour=hour) + timedelta(
        days=1
    )


# ── the contract: exactly one emission per operator action ───────────────────


async def test_a_create_through_the_module_is_announced_once_not_twice(
    engine: AsyncEngine,
) -> None:
    wiring = await _wire(engine)
    await wiring.router.create_event(tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10))
    assert wiring.bus.types() == ["calendar.event_created"]

    # the poller now observes the very same change coming back from the provider
    assert await wiring.reconciler.reconcile() == 0
    assert wiring.bus.types() == ["calendar.event_created"]


async def test_an_update_through_the_module_is_announced_once(engine: AsyncEngine) -> None:
    wiring = await _wire(engine)
    created = await wiring.router.create_event(
        tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10)
    )
    await wiring.reconciler.reconcile()
    wiring.bus.published.clear()

    await wiring.router.update_event(tenant_id=TENANT, event_id=created.id, title="Standup v2")
    assert wiring.bus.types() == ["calendar.event_updated"]
    assert await wiring.reconciler.reconcile() == 0
    assert wiring.bus.types() == ["calendar.event_updated"]


async def test_a_delete_through_the_module_is_announced_once(engine: AsyncEngine) -> None:
    wiring = await _wire(engine)
    created = await wiring.router.create_event(
        tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10)
    )
    await wiring.reconciler.reconcile()
    wiring.bus.published.clear()

    assert await wiring.router.delete_event(tenant_id=TENANT, event_id=created.id) is True
    assert wiring.bus.types() == ["calendar.event_cancelled"]
    assert await wiring.reconciler.reconcile() == 0
    assert wiring.bus.types() == ["calendar.event_cancelled"]


async def test_a_series_written_here_is_announced_once_not_once_per_occurrence(
    engine: AsyncEngine,
) -> None:
    """The series alias in the ledger. One recurring create comes back as N occurrences, all
    carrying the series id — which is the id the write seam recorded."""
    wiring = await _wire(engine)
    await wiring.router.create_event(
        tenant_id=TENANT,
        title="Weekly",
        start=_dt(9),
        end=_dt(10),
        recurrence="FREQ=WEEKLY;COUNT=3",
    )
    assert wiring.bus.types() == ["calendar.event_created"]
    assert await wiring.reconciler.reconcile() == 0
    assert wiring.bus.types() == ["calendar.event_created"]


# ── the inverse: an external change is still news ────────────────────────────


async def test_an_external_creation_is_still_announced(engine: AsyncEngine) -> None:
    """The whole point of #831 — suppression must not become a blanket mute."""
    wiring = await _wire(engine)
    wiring.google.arrives(
        Event(
            id="external-1",
            title="Booked by someone else",
            start=_dt(11),
            end=_dt(12),
            provider="google",
        )
    )
    assert await wiring.reconciler.reconcile() == 1
    assert wiring.bus.types() == ["calendar.event_created"]


async def test_an_external_deletion_is_still_announced(engine: AsyncEngine) -> None:
    wiring = await _wire(engine)
    wiring.google.arrives(
        Event(id="external-1", title="Lunch", start=_dt(12), end=_dt(13), provider="google")
    )
    await wiring.reconciler.reconcile()
    wiring.bus.published.clear()

    wiring.google.removed("external-1")
    assert await wiring.reconciler.reconcile() == 1
    assert wiring.bus.types() == ["calendar.event_cancelled"]


async def test_a_later_external_edit_of_an_event_we_wrote_is_announced(
    engine: AsyncEngine,
) -> None:
    """The marker is *consumed* on its exact-id match, so it suppresses the write it was
    recorded for and nothing after it. A ledger that only peeked would mute the operator's next
    edit for the whole TTL."""
    wiring = await _wire(engine)
    created = await wiring.router.create_event(
        tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10)
    )
    await wiring.reconciler.reconcile()  # consumes the created marker
    wiring.bus.published.clear()

    wiring.google.arrives(
        Event(
            id=created.id,
            title="Standup (moved in Google's UI)",
            start=_dt(15),
            end=_dt(16),
            provider="google",
        )
    )
    assert await wiring.reconciler.reconcile() == 1
    [envelope] = wiring.bus.envelopes()
    assert envelope.type == "calendar.event_updated"
    assert envelope.payload["time_changed"] is True


async def test_two_writes_to_one_event_each_suppress_their_own_observation(
    engine: AsyncEngine,
) -> None:
    """Consume-then-re-record: a second write re-arms the marker, so neither is re-announced."""
    wiring = await _wire(engine)
    created = await wiring.router.create_event(
        tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10)
    )
    await wiring.reconciler.reconcile()
    await wiring.router.update_event(tenant_id=TENANT, event_id=created.id, title="v2")
    await wiring.reconciler.reconcile()
    await wiring.router.update_event(tenant_id=TENANT, event_id=created.id, title="v3")
    await wiring.reconciler.reconcile()
    assert wiring.bus.types() == [
        "calendar.event_created",
        "calendar.event_updated",
        "calendar.event_updated",
    ]


# ── the ledger stays out of a local-only deployment's way ────────────────────


async def test_a_local_write_records_no_marker(engine: AsyncEngine) -> None:
    """A local-store write cannot come back through anyone's sync feed, so marking it would be
    pure churn in the commonest deployment there is."""
    # Nothing connected, so the write default really is local (with a connected account and
    # nothing enabled, #433 would prefer the connected calendar instead).
    wiring = await _wire(engine, google=_FakeGoogle(available=False), prefs=CollectionPrefs())
    created = await wiring.router.create_event(
        tenant_id=TENANT, title="Local only", start=_dt(9), end=_dt(10)
    )
    assert created.provider == "local"
    assert wiring.bus.types() == ["calendar.event_created"]
    assert (
        await wiring.ledger.peek(tenant=TENANT, key=f"calendar.event_created|local:{created.id}")
        is False
    )


async def test_a_local_only_deployment_has_no_sync_targets(engine: AsyncEngine) -> None:
    """Nothing enabled, nothing connected → no target, no provider call, an idle tick."""
    google = _FakeGoogle(available=False)
    wiring = await _wire(engine, google=google, prefs=CollectionPrefs())
    assert await wiring.router.sync_targets(tenant_id=TENANT) == []
    assert await wiring.reconciler.reconcile() == 0


async def test_a_disconnected_account_is_skipped_on_the_cheap_probe(
    engine: AsyncEngine,
) -> None:
    google = _FakeGoogle(available=False)
    wiring = await _wire(engine, google=google)
    assert await wiring.router.sync_targets(tenant_id=TENANT) == []


async def test_a_missing_provider_degrades_to_local_and_drops_out(
    engine: AsyncEngine,
) -> None:
    """#815's one rule: a stale enabled ref whose account is gone degrades to the local
    collection — which declares no sync feed, so the reconcile simply has nothing to watch."""
    local_store = LocalEventStore(engine)
    await local_store.init()
    ghost = CollectionRef(account="dropbox-calendar", collection="whatever")
    router = CollectionRouter(
        local=LocalCalendarProvider(store=local_store),
        external={},
        prefs=_Prefs(CollectionPrefs(enabled=[ghost], active=ghost)),
    )
    assert await router.sync_targets(tenant_id=TENANT) == []


async def test_sync_targets_falls_back_to_the_write_default(engine: AsyncEngine) -> None:
    """With a connected account but nothing toggled on yet, the calendar new events land on is
    still watched — a module that writes somewhere it refuses to observe would be worse than
    one that watches nothing."""
    local_store = LocalEventStore(engine)
    await local_store.init()
    google = _FakeGoogle()
    router = CollectionRouter(
        local=LocalCalendarProvider(store=local_store),
        external={"google": google},
        prefs=_Prefs(CollectionPrefs()),
    )
    targets = await router.sync_targets(tenant_id=TENANT)
    assert [(p.name, ref.account) for p, ref in targets] == [("google", "google")]


async def test_suppression_is_off_when_no_ledger_is_wired(engine: AsyncEngine) -> None:
    """The pre-#831 behaviour, still reachable: without a ledger the router records nothing and
    the reconcile suppresses nothing — two emissions, which is exactly why both are wired."""
    local_store = LocalEventStore(engine)
    await local_store.init()
    sync_store = CalendarSyncStore(engine)
    await sync_store.init()
    google = _FakeGoogle()
    bus = _RecordingBus()
    router = CollectionRouter(
        local=LocalCalendarProvider(store=local_store),
        external={"google": google},
        prefs=_Prefs(CollectionPrefs(enabled=[GOOGLE], active=GOOGLE)),
        bus=bus,  # type: ignore[arg-type]
    )
    reconciler = CalendarReconciler(
        targets=router,
        store=sync_store,
        bus=bus,  # type: ignore[arg-type]
        tenant_id=TENANT,
    )
    await reconciler.reconcile()
    await router.create_event(tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10))
    assert await reconciler.reconcile() == 1
    assert bus.types() == ["calendar.event_created", "calendar.event_created"]
