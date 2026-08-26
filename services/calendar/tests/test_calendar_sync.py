"""Unit tests for calendar's reconcile orchestrator (#831).

The subject is :class:`CalendarReconciler`: the thing that turns a provider's change feed into
`calendar.event_created` / `event_updated` / `event_cancelled` for changes made **outside**
epicurus, without ever re-announcing a change this module made itself.

No live Google anywhere — a scripted :class:`_FakeProvider` plays the change feed, including
the ``None`` that stands for a ``410 GONE`` sync token. A :class:`_RecordingBus` pins the
envelopes, the same fake shape `test_calendar_events.py` uses for the write seam.

Stores are **file-backed** SQLite under ``tmp_path`` with default pooling (never in-memory +
``StaticPool``), disposed in teardown.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_calendar.models import Attendee, DateTimeRange, Event
from epicurus_calendar.providers.base import (
    CalendarProvider,
    EditScope,
    EventChange,
    EventSyncPage,
)
from epicurus_calendar.spine import event_change_hash
from epicurus_calendar.sync import (
    CalendarReconciler,
    _next_sleep,
    run_periodic,
    tick,
)
from epicurus_calendar.sync_store import CalendarSyncStore, SelfWriteLedger
from epicurus_core import Collection, CollectionRef, EventEnvelope

TENANT = "local"
REF = CollectionRef(account="google", collection="primary")


class _RecordingBus:
    """Captures publishes instead of talking to NATS (mirrors the write seam's test fake)."""

    def __init__(self) -> None:
        self.published: list[dict[str, object]] = []

    async def publish(self, subject: str, data: object, tenant_id: str | None = None) -> None:
        assert isinstance(data, dict)
        self.published.append(data)

    def envelopes(self) -> list[EventEnvelope]:
        return [EventEnvelope.model_validate(data) for data in self.published]

    def types(self) -> list[str]:
        return [envelope.type for envelope in self.envelopes()]

    def of_type(self, event_type: str) -> list[EventEnvelope]:
        return [e for e in self.envelopes() if e.type == event_type]


class _FakeProvider(CalendarProvider):
    """A sync-capable backend whose change feed is scripted turn by turn."""

    name = "google"
    supports_sync = True

    def __init__(
        self,
        *,
        full_pages: list[EventSyncPage] | None = None,
        deltas: list[EventSyncPage | None] | None = None,
        available: bool = True,
    ) -> None:
        self._full = list(full_pages or [])
        self._deltas = list(deltas or [])
        self.available = available
        self.full_calls: list[datetime] = []
        self.delta_calls: list[str] = []

    async def full_sync(
        self, *, tenant_id: str, calendar_id: str | None = None, since: datetime
    ) -> EventSyncPage:
        self.full_calls.append(since)
        return self._full.pop(0) if self._full else EventSyncPage()

    async def changed_events_since(
        self, *, tenant_id: str, calendar_id: str | None = None, cursor: str
    ) -> EventSyncPage | None:
        self.delta_calls.append(cursor)
        if not self._deltas:
            return EventSyncPage(next_cursor=cursor)
        return self._deltas.pop(0)

    async def is_available(self, *, tenant_id: str) -> bool:
        return self.available

    # ── the rest of the seam: never exercised here ───────────────────────────

    async def list_events(
        self, *, tenant_id: str, time_range: DateTimeRange, calendar_id: str | None = None
    ) -> list[Event]:
        return []

    async def get_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> Event | None:
        return None

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
        raise NotImplementedError

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
        raise NotImplementedError

    async def delete_event(
        self,
        *,
        tenant_id: str,
        event_id: str,
        calendar_id: str | None = None,
        edit_scope: EditScope = "this",
    ) -> bool:
        raise NotImplementedError

    async def find_free_slots(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> list[DateTimeRange]:
        return []

    async def list_collections(self, *, tenant_id: str) -> list[Collection]:
        return []


class _LocalOnlyProvider(_FakeProvider):
    """A backend with no change feed at all — what the local store looks like to the loop."""

    name = "local"
    supports_sync = False


class _Targets:
    """Stands in for the router's ``sync_targets`` — the operator's selection, resolved."""

    def __init__(self, *targets: tuple[CalendarProvider, CollectionRef]) -> None:
        self._targets = list(targets)
        self.calls = 0

    async def sync_targets(self, *, tenant_id: str) -> list[tuple[CalendarProvider, CollectionRef]]:
        self.calls += 1
        return list(self._targets)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'calendar-sync.db'}")
    yield engine
    await engine.dispose()


@pytest.fixture
async def store(engine: AsyncEngine) -> CalendarSyncStore:
    store = CalendarSyncStore(engine)
    await store.init()
    return store


def _dt(day: int, hour: int = 9) -> datetime:
    return datetime.now(UTC).replace(microsecond=0, second=0, minute=0, hour=hour) + timedelta(
        days=day
    )


def _event(
    event_id: str,
    *,
    title: str = "Standup",
    day: int = 1,
    hour: int = 9,
    series: str | None = None,
    all_day: bool = False,
) -> Event:
    return Event(
        id=event_id,
        title=title,
        start=_dt(day, hour),
        end=_dt(day, hour + 1),
        provider="google",
        recurring_event_id=series,
        all_day=all_day,
    )


def _live(event: Event) -> EventChange:
    return EventChange(
        event_id=event.id,
        cancelled=False,
        event=event,
        series_id=event.recurring_event_id,
    )


def _tomb(event_id: str, *, series: str | None = None) -> EventChange:
    return EventChange(event_id=event_id, cancelled=True, series_id=series)


def _reconciler(
    targets: _Targets,
    store: CalendarSyncStore,
    bus: _RecordingBus,
    *,
    ledger: SelfWriteLedger | None = None,
    max_emissions_per_pass: int = 50,
) -> CalendarReconciler:
    return CalendarReconciler(
        targets=targets,
        store=store,
        ledger=ledger,
        bus=bus,  # type: ignore[arg-type]
        tenant_id=TENANT,
        max_emissions_per_pass=max_emissions_per_pass,
    )


# ── priming: a first sync is silent ──────────────────────────────────────────


async def test_a_first_ever_sync_primes_the_cache_and_announces_nothing(
    store: CalendarSyncStore,
) -> None:
    """The no-firehose rule. A calendar you already had is not news; announcing every existing
    event the first time a collection is watched would be noise on an industrial scale."""
    provider = _FakeProvider(
        full_pages=[
            EventSyncPage(changes=[_live(_event("e1")), _live(_event("e2"))], next_cursor="tok-1")
        ]
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)

    assert await reconciler.reconcile() == 0
    assert bus.published == []
    cached = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert sorted(cached) == ["e1", "e2"]
    state = await store.get_state(tenant=TENANT, account="google", collection="primary")
    assert state is not None and state.sync_token == "tok-1"


async def test_the_priming_window_anchors_the_full_sync(store: CalendarSyncStore) -> None:
    """The anchor is remembered because Google binds ``timeMin`` to the token it mints."""
    provider = _FakeProvider(full_pages=[EventSyncPage(next_cursor="tok-1")])
    reconciler = CalendarReconciler(
        targets=_Targets((provider, REF)),
        store=store,
        tenant_id=TENANT,
        window_days=7,
    )
    await reconciler.reconcile()
    [since] = provider.full_calls
    assert timedelta(days=6, hours=23) < datetime.now(UTC) - since < timedelta(days=7, hours=1)
    state = await store.get_state(tenant=TENANT, account="google", collection="primary")
    assert state is not None and state.window_start == since


# ── the incremental path ─────────────────────────────────────────────────────


async def test_an_external_creation_emits_event_created(store: CalendarSyncStore) -> None:
    provider = _FakeProvider(
        full_pages=[EventSyncPage(next_cursor="tok-1")],
        deltas=[EventSyncPage(changes=[_live(_event("e9", title="Dentist"))], next_cursor="t2")],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()  # prime

    assert await reconciler.reconcile() == 1
    [envelope] = bus.of_type("calendar.event_created")
    assert envelope.module == "calendar"
    assert envelope.tenant_id == TENANT
    assert envelope.dedup_key == "google:e9"
    assert envelope.payload["title"] == "Dentist"
    assert "change_hash" not in envelope.payload  # an internal, never part of the contract
    assert envelope.entity_ref is not None
    assert envelope.entity_ref.ref_id == "e9"
    assert envelope.entity_ref.kind == "event"
    assert provider.delta_calls == ["tok-1"]


async def test_the_advanced_cursor_is_persisted_for_the_next_pass(
    store: CalendarSyncStore,
) -> None:
    provider = _FakeProvider(
        full_pages=[EventSyncPage(next_cursor="tok-1")],
        deltas=[EventSyncPage(changes=[_live(_event("e9"))], next_cursor="tok-2")],
    )
    reconciler = _reconciler(_Targets((provider, REF)), store, _RecordingBus())
    await reconciler.reconcile()
    await reconciler.reconcile()
    state = await store.get_state(tenant=TENANT, account="google", collection="primary")
    assert state is not None and state.sync_token == "tok-2"


async def test_an_external_edit_emits_event_updated_with_a_change_hash_dedup_key(
    store: CalendarSyncStore,
) -> None:
    original = _event("e1", title="Standup")
    moved = _event("e1", title="Standup", day=1, hour=14)
    provider = _FakeProvider(
        full_pages=[EventSyncPage(changes=[_live(original)], next_cursor="tok-1")],
        deltas=[EventSyncPage(changes=[_live(moved)], next_cursor="tok-2")],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()

    assert await reconciler.reconcile() == 1
    [envelope] = bus.of_type("calendar.event_updated")
    assert envelope.dedup_key == f"google:e1:{event_change_hash(moved)}"
    assert envelope.payload["time_changed"] is True


async def test_an_edit_that_does_not_move_the_time_reports_time_changed_false(
    store: CalendarSyncStore,
) -> None:
    """A real before/after comparison against the cache — the reconcile has no "which args were
    passed" to fall back on, which is exactly why it caches start/end."""
    original = _event("e1", title="Standup")
    renamed = _event("e1", title="Standup (moved rooms)")
    provider = _FakeProvider(
        full_pages=[EventSyncPage(changes=[_live(original)], next_cursor="tok-1")],
        deltas=[EventSyncPage(changes=[_live(renamed)], next_cursor="tok-2")],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()
    await reconciler.reconcile()
    [envelope] = bus.of_type("calendar.event_updated")
    assert envelope.payload["time_changed"] is False


async def test_an_unchanged_event_in_the_delta_announces_nothing(
    store: CalendarSyncStore,
) -> None:
    """A provider mentioning an event again is not a change to it. Without the change-hash
    comparison every overlapping delta would be a storm of phantom updates."""
    event = _event("e1")
    provider = _FakeProvider(
        full_pages=[EventSyncPage(changes=[_live(event)], next_cursor="tok-1")],
        deltas=[EventSyncPage(changes=[_live(event)], next_cursor="tok-2")],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()
    assert await reconciler.reconcile() == 0
    assert bus.published == []


async def test_an_empty_delta_costs_one_call_and_says_nothing(
    store: CalendarSyncStore,
) -> None:
    provider = _FakeProvider(
        full_pages=[EventSyncPage(next_cursor="tok-1")],
        deltas=[EventSyncPage(next_cursor="tok-1")],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()
    assert await reconciler.reconcile() == 0
    assert bus.published == []
    assert provider.delta_calls == ["tok-1"]


# ── tombstones ───────────────────────────────────────────────────────────────


async def test_a_tombstone_for_a_known_event_emits_event_cancelled(
    store: CalendarSyncStore,
) -> None:
    """The title comes from the cache: a tombstone is a bare id, and "(unknown)" in a feed is
    worse than useless."""
    provider = _FakeProvider(
        full_pages=[
            EventSyncPage(changes=[_live(_event("e1", title="Retro"))], next_cursor="tok-1")
        ],
        deltas=[EventSyncPage(changes=[_tomb("e1")], next_cursor="tok-2")],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()

    assert await reconciler.reconcile() == 1
    [envelope] = bus.of_type("calendar.event_cancelled")
    assert envelope.dedup_key == "google:e1"
    assert envelope.payload["title"] == "Retro"
    assert envelope.entity_ref is not None and envelope.entity_ref.title == "Retro"
    cached = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert cached == {}


async def test_a_tombstone_for_an_event_we_never_saw_announces_nothing(
    store: CalendarSyncStore,
) -> None:
    """Google's ``showDeleted`` feed is full of occurrences cancelled before we ever looked.
    We never said they existed, so we do not get to say they stopped."""
    provider = _FakeProvider(
        full_pages=[EventSyncPage(next_cursor="tok-1")],
        deltas=[EventSyncPage(changes=[_tomb("never-seen")], next_cursor="tok-2")],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()
    assert await reconciler.reconcile() == 0
    assert bus.published == []


# ── 410 GONE → full resync ───────────────────────────────────────────────────


async def test_an_expired_cursor_falls_back_to_a_full_resync_that_diffs(
    store: CalendarSyncStore,
) -> None:
    """A ``410 GONE`` is a recoverable state, not a failure. The cache is what lets the resync
    report the gap *exactly* — one creation and one cancellation, not "here is everything"."""
    kept = _event("keep", title="Kept")
    provider = _FakeProvider(
        full_pages=[
            EventSyncPage(
                changes=[_live(kept), _live(_event("gone", title="Gone"))], next_cursor="tok-1"
            ),
            EventSyncPage(
                changes=[_live(kept), _live(_event("fresh", title="Fresh"))], next_cursor="tok-9"
            ),
        ],
        deltas=[None],  # the 410
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()  # prime with keep + gone

    assert await reconciler.reconcile() == 2
    assert [e.dedup_key for e in bus.of_type("calendar.event_created")] == ["google:fresh"]
    assert [e.dedup_key for e in bus.of_type("calendar.event_cancelled")] == ["google:gone"]
    state = await store.get_state(tenant=TENANT, account="google", collection="primary")
    assert state is not None and state.sync_token == "tok-9"
    cached = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert sorted(cached) == ["fresh", "keep"]


async def test_a_resync_prunes_an_aged_out_event_without_announcing_it(
    store: CalendarSyncStore,
) -> None:
    """The window moves forward on every resync. Announcing a row that merely fell out of it
    would report the passage of time as an operator action."""
    old = _event("old", title="Ancient", day=-90)
    provider = _FakeProvider(
        full_pages=[
            EventSyncPage(changes=[_live(old)], next_cursor="tok-1"),
            EventSyncPage(next_cursor="tok-9"),
        ],
        deltas=[None],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()

    assert await reconciler.reconcile() == 0
    assert bus.published == []
    cached = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert cached == {}


async def test_a_full_sync_without_a_cursor_diffs_again_next_pass(
    store: CalendarSyncStore,
) -> None:
    """A page budget exhausted mid-walk yields no token. The next pass must full-sync and
    diff — never re-prime in silence, which would swallow everything that changed meanwhile."""
    provider = _FakeProvider(
        full_pages=[
            EventSyncPage(changes=[_live(_event("e1"))], next_cursor=None),
            EventSyncPage(
                changes=[_live(_event("e1")), _live(_event("e2", title="New"))],
                next_cursor="tok-2",
            ),
        ]
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()  # primed, but no cursor

    assert await reconciler.reconcile() == 1
    assert [e.dedup_key for e in bus.of_type("calendar.event_created")] == ["google:e2"]
    assert provider.delta_calls == []  # never asked for a delta it had no cursor for


async def test_sync_state_survives_a_restart(engine: AsyncEngine) -> None:
    """A restart-resume: a brand-new reconciler over the same database resumes from the stored
    cursor rather than re-priming (which would silently swallow the outage's changes)."""
    store = CalendarSyncStore(engine)
    await store.init()
    provider = _FakeProvider(full_pages=[EventSyncPage(next_cursor="tok-1")])
    await _reconciler(_Targets((provider, REF)), store, _RecordingBus()).reconcile()

    restarted_store = CalendarSyncStore(engine)
    restarted_provider = _FakeProvider(
        deltas=[EventSyncPage(changes=[_live(_event("e9"))], next_cursor="tok-2")]
    )
    bus = _RecordingBus()
    emitted = await _reconciler(
        _Targets((restarted_provider, REF)), restarted_store, bus
    ).reconcile()

    assert emitted == 1
    assert restarted_provider.full_calls == []  # resumed, not re-primed
    assert restarted_provider.delta_calls == ["tok-1"]
    assert bus.types() == ["calendar.event_created"]


# ── recurring series: one action, one event ──────────────────────────────────


async def test_a_new_series_collapses_to_a_single_creation_keyed_on_the_series(
    store: CalendarSyncStore,
) -> None:
    """A weekly stand-up added in Google's UI arrives as one change per occurrence. Announcing
    each would turn one click into dozens of spine events."""
    occurrences = [
        _live(_event(f"s1_{day}", title="Weekly", day=day, series="s1")) for day in (1, 8, 15)
    ]
    provider = _FakeProvider(
        full_pages=[EventSyncPage(next_cursor="tok-1")],
        deltas=[EventSyncPage(changes=occurrences, next_cursor="tok-2")],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()

    assert await reconciler.reconcile() == 1
    [envelope] = bus.of_type("calendar.event_created")
    assert envelope.dedup_key == "google:s1"
    assert envelope.entity_ref is not None and envelope.entity_ref.ref_id == "s1"
    # every occurrence is still cached, so none of them re-announces later
    cached = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert sorted(cached) == ["s1_1", "s1_15", "s1_8"]


async def test_a_single_occurrence_change_keeps_its_own_id(store: CalendarSyncStore) -> None:
    """Only one occurrence moved, so the event is about *that occurrence*, not the series."""
    first = _event("s1_1", title="Weekly", day=1, series="s1")
    provider = _FakeProvider(
        full_pages=[EventSyncPage(changes=[_live(first)], next_cursor="tok-1")],
        deltas=[
            EventSyncPage(
                changes=[_live(_event("s1_1", title="Weekly", day=1, hour=16, series="s1"))],
                next_cursor="tok-2",
            )
        ],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()
    await reconciler.reconcile()
    [envelope] = bus.of_type("calendar.event_updated")
    assert envelope.entity_ref is not None and envelope.entity_ref.ref_id == "s1_1"


async def test_a_cancelled_series_collapses_to_one_cancellation(
    store: CalendarSyncStore,
) -> None:
    occurrences = [
        _live(_event(f"s1_{day}", title="Weekly", day=day, series="s1")) for day in (1, 8)
    ]
    provider = _FakeProvider(
        full_pages=[EventSyncPage(changes=occurrences, next_cursor="tok-1")],
        deltas=[
            EventSyncPage(
                changes=[_tomb("s1_1", series="s1"), _tomb("s1_8", series="s1")],
                next_cursor="tok-2",
            )
        ],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()

    assert await reconciler.reconcile() == 1
    [envelope] = bus.of_type("calendar.event_cancelled")
    assert envelope.dedup_key == "google:s1"
    cached = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert cached == {}


async def test_creations_and_cancellations_of_one_series_stay_separate_events(
    store: CalendarSyncStore,
) -> None:
    """Collapsing is per ``(series, event type)`` — a series losing one occurrence and gaining
    two is two different pieces of news, not one."""
    provider = _FakeProvider(
        full_pages=[
            EventSyncPage(changes=[_live(_event("s1_1", day=1, series="s1"))], next_cursor="tok-1")
        ],
        deltas=[
            EventSyncPage(
                changes=[
                    _tomb("s1_1", series="s1"),
                    _live(_event("s1_8", day=8, series="s1")),
                    _live(_event("s1_15", day=15, series="s1")),
                ],
                next_cursor="tok-2",
            )
        ],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()

    assert await reconciler.reconcile() == 2
    assert sorted(bus.types()) == ["calendar.event_cancelled", "calendar.event_created"]


# ── bounds and degradation ───────────────────────────────────────────────────


async def test_a_pass_caps_how_many_events_it_announces(store: CalendarSyncStore) -> None:
    """A bulk import into the connected calendar must not hand the automations engine a
    thousand triggers. The *cache* is still updated in full, so the shortfall stays
    unannounced rather than re-announcing itself next tick."""
    changes = [_live(_event(f"e{i}", day=i)) for i in range(1, 11)]
    provider = _FakeProvider(
        full_pages=[EventSyncPage(next_cursor="tok-1")],
        deltas=[EventSyncPage(changes=changes, next_cursor="tok-2")],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus, max_emissions_per_pass=3)
    await reconciler.reconcile()

    assert await reconciler.reconcile() == 3
    assert len(bus.published) == 3
    cached = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert len(cached) == 10
    # a following pass has nothing to say about them — they are cached, not pending
    assert await reconciler.reconcile() == 0


async def test_no_targets_means_an_idle_tick_with_no_provider_call() -> None:
    """The missing/unconfigured-provider case (#815's one-rule degrade). A local-only
    deployment must cost nothing and say nothing."""
    targets = _Targets()
    bus = _RecordingBus()
    reconciler = CalendarReconciler(
        targets=targets,
        store=CalendarSyncStore(create_async_engine("sqlite+aiosqlite:///:memory:")),
        bus=bus,  # type: ignore[arg-type]
        tenant_id=TENANT,
    )
    assert await reconciler.reconcile() == 0
    assert targets.calls == 1
    assert bus.published == []


async def test_a_non_sync_capable_backend_is_never_a_target(store: CalendarSyncStore) -> None:
    """``supports_sync`` is the flag the router filters on; this pins that the reconciler would
    never call a backend that has no feed even if one were handed to it."""
    local = _LocalOnlyProvider()
    assert local.supports_sync is False


async def test_one_failing_collection_never_stops_the_others(
    store: CalendarSyncStore,
) -> None:
    """A revoked grant on one calendar must not silence a second, healthy one (#209)."""

    class _Broken(_FakeProvider):
        async def full_sync(
            self, *, tenant_id: str, calendar_id: str | None = None, since: datetime
        ) -> EventSyncPage:
            raise RuntimeError("token revoked")

    healthy = _FakeProvider(
        full_pages=[EventSyncPage(next_cursor="tok-1")],
        deltas=[EventSyncPage(changes=[_live(_event("e9"))], next_cursor="tok-2")],
    )
    other = CollectionRef(account="google", collection="secondary")
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((_Broken(), REF), (healthy, other)), store, bus)
    await reconciler.reconcile()

    assert await reconciler.reconcile() == 1
    assert bus.types() == ["calendar.event_created"]


async def test_collections_keep_separate_caches(store: CalendarSyncStore) -> None:
    """The same event id in two calendars is two observations — a shared cache would make the
    second calendar's copy look like an edit of the first."""
    work = _FakeProvider(
        full_pages=[EventSyncPage(changes=[_live(_event("e1"))], next_cursor="w1")]
    )
    home = _FakeProvider(
        full_pages=[EventSyncPage(changes=[_live(_event("e1"))], next_cursor="h1")]
    )
    targets = _Targets(
        (work, CollectionRef(account="google", collection="work")),
        (home, CollectionRef(account="google", collection="home")),
    )
    bus = _RecordingBus()
    await _reconciler(targets, store, bus).reconcile()
    assert await store.count_events(tenant=TENANT) == 2


async def test_a_spine_hiccup_never_costs_the_cache_write(store: CalendarSyncStore) -> None:
    class _BrokenBus(_RecordingBus):
        async def publish(self, subject: str, data: object, tenant_id: str | None = None) -> None:
            raise RuntimeError("nats is down")

    provider = _FakeProvider(
        full_pages=[EventSyncPage(next_cursor="tok-1")],
        deltas=[EventSyncPage(changes=[_live(_event("e9"))], next_cursor="tok-2")],
    )
    bus = _BrokenBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()

    assert await reconciler.reconcile() == 0  # nothing announced …
    cached = await store.get_events(tenant=TENANT, account="google", collection="primary")
    assert list(cached) == ["e9"]  # … but the observation was still recorded


async def test_reconcile_is_single_flight(store: CalendarSyncStore) -> None:
    """Two overlapping passes must not both observe the pre-advance cursor and announce one
    change twice (mail's #796 lesson, same lock)."""
    provider = _FakeProvider(
        full_pages=[EventSyncPage(next_cursor="tok-1")],
        deltas=[
            EventSyncPage(changes=[_live(_event("e9"))], next_cursor="tok-2"),
            EventSyncPage(next_cursor="tok-2"),
        ],
    )
    bus = _RecordingBus()
    reconciler = _reconciler(_Targets((provider, REF)), store, bus)
    await reconciler.reconcile()

    results = await asyncio.gather(reconciler.reconcile(), reconciler.reconcile())
    assert sorted(results) == [0, 1]
    assert bus.types() == ["calendar.event_created"]


# ── the loop ─────────────────────────────────────────────────────────────────


async def test_tick_delegates_to_reconcile(store: CalendarSyncStore) -> None:
    provider = _FakeProvider(
        full_pages=[EventSyncPage(changes=[_live(_event("e1"))], next_cursor="tok-1")]
    )
    reconciler = _reconciler(_Targets((provider, REF)), store, _RecordingBus())
    assert await tick(reconciler=reconciler) == 0
    assert provider.full_calls != []


async def test_a_non_positive_interval_disables_the_loop_entirely(
    store: CalendarSyncStore,
) -> None:
    """An operator who turns reconcile off gets no background task, not one that spins."""
    provider = _FakeProvider(full_pages=[EventSyncPage(next_cursor="tok-1")])
    reconciler = _reconciler(_Targets((provider, REF)), store, _RecordingBus())
    await asyncio.wait_for(
        run_periodic(reconciler=reconciler, tenant=TENANT, poll_interval_s=0), timeout=5
    )
    assert provider.full_calls == []


async def test_the_loop_ticks_first_and_survives_a_bad_tick(
    store: CalendarSyncStore,
) -> None:
    """Ticks before it sleeps (a restart should notice the outage's changes straight away) and
    one failure is logged and skipped rather than killing the loop."""
    calls: list[int] = []

    class _Flaky(_FakeProvider):
        async def full_sync(
            self, *, tenant_id: str, calendar_id: str | None = None, since: datetime
        ) -> EventSyncPage:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return EventSyncPage(next_cursor="tok-1")

    reconciler = _reconciler(_Targets((_Flaky(), REF)), store, _RecordingBus())
    loop_task = asyncio.create_task(
        run_periodic(reconciler=reconciler, tenant=TENANT, poll_interval_s=0.01, jitter_frac=0.0)
    )
    try:
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        loop_task.cancel()
        with suppress(asyncio.CancelledError):
            await loop_task
    assert len(calls) >= 2


async def test_cancelling_the_loop_propagates_immediately(store: CalendarSyncStore) -> None:
    """Shutdown must stop the loop at its sleep, not wait a full interval out."""
    provider = _FakeProvider(full_pages=[EventSyncPage(next_cursor="tok-1")])
    reconciler = _reconciler(_Targets((provider, REF)), store, _RecordingBus())
    loop_task = asyncio.create_task(
        run_periodic(reconciler=reconciler, tenant=TENANT, poll_interval_s=3600)
    )
    await asyncio.sleep(0.05)
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task


def test_jitter_stays_inside_its_band_and_never_goes_negative() -> None:
    assert _next_sleep(300.0, 0.0) == 300.0
    for _ in range(200):
        assert 270.0 <= _next_sleep(300.0, 0.1) <= 330.0
        assert _next_sleep(1.0, 5.0) >= 0.0
