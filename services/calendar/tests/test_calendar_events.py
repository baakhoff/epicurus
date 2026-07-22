"""Unit tests for calendar's module-event-spine emission (#664, ADR-0103).

Two seams: :class:`CollectionRouter`'s create/update/delete_event (the provider-write seam)
and the lead-time scheduler (:mod:`epicurus_calendar.scheduler`, the module's first periodic
background job). A :class:`_RecordingBus` fake pins the emitted envelopes, mirroring the
pattern already used for echo's and mail's own event-spine tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_calendar.db import LocalEventStore
from epicurus_calendar.lead_time_prefs import LeadTimePrefsStore
from epicurus_calendar.providers.local import LocalCalendarProvider
from epicurus_calendar.providers.router import CollectionRouter
from epicurus_calendar.scheduler import FiredMarkerStore, tick
from epicurus_core import CollectionPrefs, EventEnvelope

TENANT = "local"


class _RecordingBus:
    """Captures publishes instead of talking to NATS (mirrors echo's/mail's test fakes)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object], str | None]] = []

    async def publish(self, subject: str, data: object, tenant_id: str | None = None) -> None:
        assert isinstance(data, dict)
        self.published.append((subject, data, tenant_id))

    def envelopes(self) -> list[EventEnvelope]:
        return [EventEnvelope.model_validate(data) for _, data, _ in self.published]

    def envelopes_of_type(self, event_type: str) -> list[EventEnvelope]:
        return [e for e in self.envelopes() if e.type == event_type]


class _StaticPrefs:
    async def get_collections(self) -> CollectionPrefs:
        return CollectionPrefs()


async def _router_with_bus() -> tuple[CollectionRouter, _RecordingBus]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = LocalEventStore(engine)
    await store.init()
    local = LocalCalendarProvider(store=store)
    bus = _RecordingBus()
    router = CollectionRouter(
        local=local,
        external={},
        prefs=_StaticPrefs(),
        bus=bus,  # type: ignore[arg-type]
    )
    return router, bus


def _dt(hour: int, *, day: int = 15) -> datetime:
    return datetime(2025, 6, day, hour, 0, 0, tzinfo=UTC)


# ── router emission: event_created ───────────────────────────────────────────


async def test_create_event_emits_event_created() -> None:
    router, bus = await _router_with_bus()
    event = await router.create_event(tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10))
    [envelope] = bus.envelopes_of_type("calendar.event_created")
    assert envelope.module == "calendar"
    assert envelope.dedup_key == f"local:{event.id}"
    assert envelope.payload["title"] == "Standup"
    assert envelope.payload["all_day"] is False
    assert envelope.entity_ref is not None
    assert envelope.entity_ref.ref_id == event.id
    assert envelope.entity_ref.kind == "event"


async def test_create_event_payload_never_carries_description() -> None:
    router, bus = await _router_with_bus()
    await router.create_event(
        tenant_id=TENANT,
        title="Standup",
        start=_dt(9),
        end=_dt(10),
        description="a secret agenda nobody else should see in a feed",
    )
    [envelope] = bus.envelopes_of_type("calendar.event_created")
    assert "description" not in envelope.payload
    assert "secret agenda" not in str(envelope.payload)


async def test_no_bus_skips_emission_without_error() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = LocalEventStore(engine)
    await store.init()
    local = LocalCalendarProvider(store=store)
    router = CollectionRouter(local=local, external={}, prefs=_StaticPrefs())  # type: ignore[arg-type]
    await router.create_event(tenant_id=TENANT, title="x", start=_dt(9), end=_dt(10))  # no raise


# ── router emission: event_updated ───────────────────────────────────────────


async def test_update_event_flags_a_real_time_change() -> None:
    router, bus = await _router_with_bus()
    created = await router.create_event(
        tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10)
    )
    bus.published.clear()
    await router.update_event(tenant_id=TENANT, event_id=created.id, start=_dt(11), end=_dt(12))
    [envelope] = bus.envelopes_of_type("calendar.event_updated")
    assert envelope.payload["time_changed"] is True


async def test_update_event_does_not_flag_a_title_only_edit() -> None:
    router, bus = await _router_with_bus()
    created = await router.create_event(
        tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10)
    )
    bus.published.clear()
    await router.update_event(tenant_id=TENANT, event_id=created.id, title="Standup (moved room)")
    [envelope] = bus.envelopes_of_type("calendar.event_updated")
    assert envelope.payload["time_changed"] is False
    assert envelope.payload["title"] == "Standup (moved room)"


async def test_update_event_dedup_key_changes_with_the_edit() -> None:
    # "dedup provider id + change hash" (#664): two DIFFERENT edits must not collide in the
    # core's dedup-on-(tenant, module, dedup_key) log.
    router, bus = await _router_with_bus()
    created = await router.create_event(
        tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10)
    )
    bus.published.clear()
    await router.update_event(tenant_id=TENANT, event_id=created.id, title="First edit")
    await router.update_event(tenant_id=TENANT, event_id=created.id, title="Second edit")
    keys = [e.dedup_key for e in bus.envelopes_of_type("calendar.event_updated")]
    assert len(keys) == 2
    assert keys[0] != keys[1]


async def test_update_event_dedup_key_is_stable_for_the_identical_edit() -> None:
    # The other half of "dedup ... + change hash": the SAME resulting state must reuse one key
    # so a retried write dedups rather than double-logging.
    router, bus = await _router_with_bus()
    created = await router.create_event(
        tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10)
    )
    bus.published.clear()
    await router.update_event(tenant_id=TENANT, event_id=created.id, title="Renamed")
    await router.update_event(tenant_id=TENANT, event_id=created.id, title="Renamed")
    keys = [e.dedup_key for e in bus.envelopes_of_type("calendar.event_updated")]
    assert keys[0] == keys[1]


async def test_updating_an_untracked_event_still_infers_time_changed_from_the_args() -> None:
    # When `before` can't be resolved (get_event misses), the flag degrades to "were start/end
    # actually passed" rather than silently reading False.
    router, bus = await _router_with_bus()
    result = await router.update_event(
        tenant_id=TENANT, event_id="ghost", start=_dt(9), end=_dt(10)
    )
    assert result is None  # nothing to update
    assert bus.envelopes_of_type("calendar.event_updated") == []  # and nothing emitted either


# ── router emission: event_cancelled ─────────────────────────────────────────


async def test_delete_event_emits_event_cancelled() -> None:
    router, bus = await _router_with_bus()
    created = await router.create_event(
        tenant_id=TENANT, title="Standup", start=_dt(9), end=_dt(10)
    )
    bus.published.clear()
    ok = await router.delete_event(tenant_id=TENANT, event_id=created.id)
    assert ok is True
    [envelope] = bus.envelopes_of_type("calendar.event_cancelled")
    assert envelope.dedup_key == f"local:{created.id}"
    assert envelope.payload["title"] == "Standup"


async def test_deleting_a_missing_event_emits_nothing() -> None:
    router, bus = await _router_with_bus()
    ok = await router.delete_event(tenant_id=TENANT, event_id="ghost")
    assert ok is False
    assert bus.envelopes_of_type("calendar.event_cancelled") == []


# ── scheduler: event_starting_soon / event_ended (#664) ──────────────────────


async def _scheduler_fixtures() -> tuple[
    LocalCalendarProvider, LeadTimePrefsStore, FiredMarkerStore
]:
    events_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = LocalEventStore(events_engine)
    await store.init()
    provider = LocalCalendarProvider(store=store)
    lead_prefs = LeadTimePrefsStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await lead_prefs.init()
    markers = FiredMarkerStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await markers.init()
    return provider, lead_prefs, markers


async def test_starting_soon_fires_for_an_event_inside_the_lead_window() -> None:
    provider, lead_prefs, markers = await _scheduler_fixtures()
    now = datetime.now(UTC)
    event = await provider.create_event(
        tenant_id=TENANT,
        title="Soon",
        start=now + timedelta(minutes=10),
        end=now + timedelta(minutes=40),
    )
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=provider,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        now=now,  # type: ignore[arg-type]
    )
    [envelope] = bus.envelopes_of_type("calendar.event_starting_soon")
    assert envelope.dedup_key == f"local:{event.id}:starting_soon"
    assert envelope.payload["lead_minutes"] == 15


async def test_starting_soon_does_not_fire_outside_the_lead_window() -> None:
    provider, lead_prefs, markers = await _scheduler_fixtures()
    now = datetime.now(UTC)
    await provider.create_event(
        tenant_id=TENANT,
        title="Later",
        start=now + timedelta(hours=3),
        end=now + timedelta(hours=4),
    )
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=provider,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        now=now,  # type: ignore[arg-type]
    )
    assert bus.envelopes_of_type("calendar.event_starting_soon") == []


async def test_starting_soon_fires_only_once_across_ticks() -> None:
    provider, lead_prefs, markers = await _scheduler_fixtures()
    now = datetime.now(UTC)
    await provider.create_event(
        tenant_id=TENANT,
        title="Soon",
        start=now + timedelta(minutes=10),
        end=now + timedelta(minutes=40),
    )
    bus = _RecordingBus()
    for _ in range(3):
        await tick(
            tenant=TENANT,
            provider=provider,
            lead_prefs=lead_prefs,
            markers=markers,
            bus=bus,
            now=now,  # type: ignore[arg-type]
        )
    assert len(bus.envelopes_of_type("calendar.event_starting_soon")) == 1


async def test_starting_soon_survives_a_process_restart() -> None:
    # Fire-once must be durable, not in-memory: a FRESH FiredMarkerStore instance over the
    # SAME underlying database must still see the earlier claim.
    marker_engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    first = FiredMarkerStore(marker_engine)
    await first.init()
    assert await first.try_claim(tenant=TENANT, event_id="local:e1", marker="starting_soon")

    reopened = FiredMarkerStore(marker_engine)  # a fresh instance, simulating a process restart
    assert await reopened.has_fired(tenant=TENANT, event_id="local:e1", marker="starting_soon")
    assert not await reopened.try_claim(tenant=TENANT, event_id="local:e1", marker="starting_soon")


async def test_starting_soon_honors_a_custom_lead_time() -> None:
    provider, lead_prefs, markers = await _scheduler_fixtures()
    await lead_prefs.set_lead_minutes(TENANT, 5)
    now = datetime.now(UTC)
    await provider.create_event(
        tenant_id=TENANT,
        title="Soon-ish",
        start=now + timedelta(minutes=10),
        end=now + timedelta(minutes=40),
    )
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=provider,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        now=now,  # type: ignore[arg-type]
    )
    # 10 minutes out, 5-minute lead → not yet due.
    assert bus.envelopes_of_type("calendar.event_starting_soon") == []


async def test_event_ended_fires_after_the_end_time_passes() -> None:
    provider, lead_prefs, markers = await _scheduler_fixtures()
    now = datetime.now(UTC)
    event = await provider.create_event(
        tenant_id=TENANT,
        title="Just ended",
        start=now - timedelta(minutes=30),
        end=now - timedelta(minutes=5),
    )
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=provider,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        now=now,  # type: ignore[arg-type]
    )
    [envelope] = bus.envelopes_of_type("calendar.event_ended")
    assert envelope.dedup_key == f"local:{event.id}:ended"


async def test_event_ended_does_not_fire_for_a_still_running_event() -> None:
    provider, lead_prefs, markers = await _scheduler_fixtures()
    now = datetime.now(UTC)
    await provider.create_event(
        tenant_id=TENANT,
        title="Ongoing",
        start=now - timedelta(minutes=10),
        end=now + timedelta(minutes=10),
    )
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=provider,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        now=now,  # type: ignore[arg-type]
    )
    assert bus.envelopes_of_type("calendar.event_ended") == []


async def test_a_starting_soon_event_does_not_also_fire_ended() -> None:
    provider, lead_prefs, markers = await _scheduler_fixtures()
    now = datetime.now(UTC)
    await provider.create_event(
        tenant_id=TENANT,
        title="Soon",
        start=now + timedelta(minutes=10),
        end=now + timedelta(minutes=40),
    )
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=provider,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        now=now,  # type: ignore[arg-type]
    )
    assert bus.envelopes_of_type("calendar.event_ended") == []
    assert len(bus.envelopes_of_type("calendar.event_starting_soon")) == 1
