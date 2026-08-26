"""Integration test: a change made outside epicurus reaches a subscriber. Requires Docker.

Every unit test in this suite proves the reconcile loop *emits* — against a fake bus, which
will happily accept a subject a real broker would route somewhere else entirely. What only a
real server can prove is the rest of the sentence: that the event the background loop publishes
lands on the subject the core's intake actually listens to, and therefore reaches an
automation. That is the whole point of #831 — before it, an automation on "a calendar change"
could only ever fire for changes the operator made *through* epicurus.

So this subscribes with the intake's own cross-tenant wildcard (``events.>``, see
``EventIntake.start``) rather than importing core-app — a module must not depend on the core's
package — and asserts a subscriber standing in for the automation matcher receives the event.
Nothing calls an MCP tool or an HTTP endpoint here; the only driver is the loop.

The stores are **file-backed** SQLite, not in-memory + ``StaticPool``: the loop writes from its
own task while the test waits on the NATS callback task, and ``StaticPool`` shares one DBAPI
connection across every session, so a checkout-return ``ROLLBACK`` can land inside a concurrent
writer's transaction and silently erase it. A file database gives each session its own
connection, which is what production Postgres does anyway.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from epicurus_calendar.models import Attendee, DateTimeRange
from epicurus_calendar.models import Event as CalendarEvent
from epicurus_calendar.providers.base import (
    CalendarProvider,
    EditScope,
    EventChange,
    EventSyncPage,
)
from epicurus_calendar.sync import CalendarReconciler, run_periodic
from epicurus_calendar.sync_store import CalendarSyncStore, SelfWriteLedger
from epicurus_core import Collection, CollectionRef, Event, EventBus, EventEnvelope

pytestmark = pytest.mark.integration

TENANT = "local"
REF = CollectionRef(account="google", collection="primary")


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    container = DockerContainer("nats:2.10").with_command("-js").with_exposed_ports(4222)
    with container:
        wait_for_logs(container, "Server is ready")
        yield f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'calendar-sync.db'}")
    yield engine
    # Dispose before the test's loop closes: each aiosqlite connection owns a worker thread,
    # and an undisposed engine leaves them raising "Event loop is closed" at GC.
    await engine.dispose()


class _ScriptedGoogle(CalendarProvider):
    """A backend whose change feed reports whatever the test says the operator did in Google."""

    name = "google"
    supports_sync = True

    def __init__(self) -> None:
        self.pending: list[EventChange] = []
        self.full_calls = 0
        self.delta_calls: list[str] = []

    def arrives(self, event: CalendarEvent) -> None:
        self.pending.append(EventChange(event_id=event.id, event=event))

    async def full_sync(
        self, *, tenant_id: str, calendar_id: str | None = None, since: datetime
    ) -> EventSyncPage:
        self.full_calls += 1
        return EventSyncPage(next_cursor="tok-1")

    async def changed_events_since(
        self, *, tenant_id: str, calendar_id: str | None = None, cursor: str
    ) -> EventSyncPage | None:
        self.delta_calls.append(cursor)
        changes = list(self.pending)
        self.pending.clear()
        return EventSyncPage(changes=changes, next_cursor="tok-2")

    async def is_available(self, *, tenant_id: str) -> bool:
        return True

    async def list_events(
        self, *, tenant_id: str, time_range: DateTimeRange, calendar_id: str | None = None
    ) -> list[CalendarEvent]:
        return []

    async def get_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> CalendarEvent | None:
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
    ) -> CalendarEvent:
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
    ) -> CalendarEvent | None:
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


class _Targets:
    def __init__(self, provider: CalendarProvider) -> None:
        self._provider = provider

    async def sync_targets(self, *, tenant_id: str) -> list[tuple[CalendarProvider, CollectionRef]]:
        return [(self._provider, REF)]


def _event(event_id: str, *, title: str) -> CalendarEvent:
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    return CalendarEvent(
        id=event_id, title=title, start=start, end=start + timedelta(hours=1), provider="google"
    )


async def _await_envelope(
    bus: EventBus, delivered: asyncio.Queue[EventEnvelope], wanted: str
) -> None:
    async def _automation(event: Event) -> None:
        """Stands in for the core's intake → automation matcher, wildcard and all."""
        envelope = EventEnvelope.model_validate(event.json())
        if envelope.type == wanted:
            await delivered.put(envelope)

    await bus.subscribe_any_tenant("events.>", _automation)
    await bus.client.flush()


async def test_an_external_calendar_change_reaches_a_subscriber(
    nats_url: str, engine: AsyncEngine
) -> None:
    """emit → NATS → a ``calendar.event_created`` subscriber, driven only by the loop (#831).

    This is the chain an automation on "something changed in my calendar" rides. Before the
    reconcile layer existed it could only ever start from a write made through this module,
    which is why an event booked into the operator's Google calendar by anyone else was
    invisible to the whole system.
    """
    store = CalendarSyncStore(engine)
    await store.init()
    ledger = SelfWriteLedger(engine, ttl_s=900.0)
    await ledger.init()
    provider = _ScriptedGoogle()
    delivered: asyncio.Queue[EventEnvelope] = asyncio.Queue()

    async with EventBus(nats_url) as bus:
        await _await_envelope(bus, delivered, "calendar.event_created")
        reconciler = CalendarReconciler(
            targets=_Targets(provider),
            store=store,
            ledger=ledger,
            bus=bus,
            tenant_id=TENANT,
        )
        await reconciler.reconcile()  # prime: silent, by design
        provider.arrives(_event("ext-1", title="Booked by someone else"))

        loop_task = asyncio.create_task(
            run_periodic(
                reconciler=reconciler, tenant=TENANT, poll_interval_s=0.05, jitter_frac=0.0
            )
        )
        try:
            envelope = await asyncio.wait_for(delivered.get(), timeout=20)
        finally:
            loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await loop_task

    assert envelope.tenant_id == TENANT  # tenant-scoped end to end (constraint #1)
    assert envelope.module == "calendar"
    assert envelope.dedup_key == "google:ext-1"
    assert envelope.payload["title"] == "Booked by someone else"
    assert envelope.entity_ref is not None
    assert envelope.entity_ref.ref_id == "ext-1"
    assert envelope.entity_ref.kind == "event"


async def test_sync_state_resumes_after_a_restart(nats_url: str, engine: AsyncEngine) -> None:
    """A restarted module resumes from its stored cursor instead of re-priming.

    Re-priming would be silent by design, so the failure this guards is the worst kind: every
    change made during the outage would be absorbed into a "first sync" and never announced.
    The assertion is therefore both halves — the second instance never full-syncs, *and* the
    change that landed while it was down reaches a real subscriber.
    """
    store = CalendarSyncStore(engine)
    await store.init()
    ledger = SelfWriteLedger(engine, ttl_s=900.0)
    await ledger.init()
    first_boot = _ScriptedGoogle()
    delivered: asyncio.Queue[EventEnvelope] = asyncio.Queue()

    async with EventBus(nats_url) as bus:
        await _await_envelope(bus, delivered, "calendar.event_created")
        await CalendarReconciler(
            targets=_Targets(first_boot),
            store=store,
            ledger=ledger,
            bus=bus,
            tenant_id=TENANT,
        ).reconcile()
        assert first_boot.full_calls == 1

        # …the service restarts: brand-new store handle, reconciler and provider client, one
        # database. Meanwhile the operator moved something in Google's UI.
        restarted_store = CalendarSyncStore(engine)
        second_boot = _ScriptedGoogle()
        second_boot.arrives(_event("ext-2", title="Added during the outage"))
        reconciler = CalendarReconciler(
            targets=_Targets(second_boot),
            store=restarted_store,
            ledger=SelfWriteLedger(engine, ttl_s=900.0),
            bus=bus,
            tenant_id=TENANT,
        )
        loop_task = asyncio.create_task(
            run_periodic(
                reconciler=reconciler, tenant=TENANT, poll_interval_s=0.05, jitter_frac=0.0
            )
        )
        try:
            envelope = await asyncio.wait_for(delivered.get(), timeout=20)
        finally:
            loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await loop_task

    assert envelope.dedup_key == "google:ext-2"
    assert second_boot.full_calls == 0  # resumed, never re-primed
    assert second_boot.delta_calls[0] == "tok-1"  # …from the cursor the first boot stored
