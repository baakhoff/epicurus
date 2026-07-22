"""Unit tests for tasks' module-event-spine emission (#664, ADR-0103).

Two seams: :class:`TasksRouter`'s add/complete/update_task + `_move_task` (the provider-write
seam) and the lead-time scheduler (:mod:`epicurus_tasks.scheduler`, the module's first periodic
background job). A :class:`_RecordingBus` fake pins the emitted envelopes, mirroring the pattern
already used for echo's, mail's, and calendar's own event-spine tests.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import CollectionPrefs, CollectionRef, EventEnvelope
from epicurus_tasks.db import TaskStore
from epicurus_tasks.lead_time_prefs import LeadTimePrefsStore
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.router import TasksRouter
from epicurus_tasks.scheduler import FiredMarkerStore, tick

TENANT = "local"


class _RecordingBus:
    """Captures publishes instead of talking to NATS (mirrors the other modules' test fakes)."""

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
    def __init__(self, prefs: CollectionPrefs | None = None) -> None:
        self._prefs = prefs or CollectionPrefs()

    async def get_collections(self) -> CollectionPrefs:
        return self._prefs


async def _local_provider() -> LocalTasksProvider:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = TaskStore(engine)
    await store.init()
    return LocalTasksProvider(store)


async def _router_with_bus() -> tuple[TasksRouter, LocalTasksProvider, _RecordingBus]:
    local = await _local_provider()
    bus = _RecordingBus()
    router = TasksRouter(
        local=local,
        external={},
        prefs=_StaticPrefs(),
        bus=bus,  # type: ignore[arg-type]
    )
    return router, local, bus


# ── router emission: task_created ────────────────────────────────────────────


async def test_add_task_emits_task_created() -> None:
    router, _, bus = await _router_with_bus()
    task = await router.add_task(TENANT, "Ship PR")
    [envelope] = bus.envelopes_of_type("tasks.task_created")
    assert envelope.module == "tasks"
    assert envelope.dedup_key == f"local:{task.id}"
    assert envelope.payload["title"] == "Ship PR"
    assert envelope.entity_ref is not None
    assert envelope.entity_ref.ref_id == task.id
    assert envelope.entity_ref.kind == "task"


async def test_task_created_payload_never_carries_notes() -> None:
    router, _, bus = await _router_with_bus()
    await router.add_task(TENANT, "Ship PR", notes="internal reasoning nobody else should see")
    [envelope] = bus.envelopes_of_type("tasks.task_created")
    assert "notes" not in envelope.payload
    assert "internal reasoning" not in str(envelope.payload)


async def test_no_bus_skips_emission_without_error() -> None:
    local = await _local_provider()
    router = TasksRouter(local=local, external={}, prefs=_StaticPrefs())  # type: ignore[arg-type]
    await router.add_task(TENANT, "x")  # must not raise


# ── router emission: task_completed ──────────────────────────────────────────


async def test_complete_task_emits_task_completed() -> None:
    router, _, bus = await _router_with_bus()
    task = await router.add_task(TENANT, "Ship PR")
    bus.published.clear()
    done = await router.complete_task(TENANT, task.id)
    assert done.status == "done"
    [envelope] = bus.envelopes_of_type("tasks.task_completed")
    assert envelope.dedup_key == f"local:{task.id}"
    assert envelope.payload["status"] == "done"


# ── router emission: task_updated ────────────────────────────────────────────


async def test_update_task_emits_task_updated() -> None:
    router, _, bus = await _router_with_bus()
    task = await router.add_task(TENANT, "Ship PR")
    bus.published.clear()
    await router.update_task(TENANT, task.id, title="Ship PR (v2)")
    [envelope] = bus.envelopes_of_type("tasks.task_updated")
    assert envelope.payload["title"] == "Ship PR (v2)"


async def test_update_task_dedup_key_changes_with_the_edit() -> None:
    router, _, bus = await _router_with_bus()
    task = await router.add_task(TENANT, "Ship PR")
    bus.published.clear()
    await router.update_task(TENANT, task.id, title="First edit")
    await router.update_task(TENANT, task.id, title="Second edit")
    keys = [e.dedup_key for e in bus.envelopes_of_type("tasks.task_updated")]
    assert len(keys) == 2
    assert keys[0] != keys[1]


async def test_update_task_dedup_key_is_stable_for_the_identical_edit() -> None:
    router, _, bus = await _router_with_bus()
    task = await router.add_task(TENANT, "Ship PR")
    bus.published.clear()
    await router.update_task(TENANT, task.id, title="Renamed")
    await router.update_task(TENANT, task.id, title="Renamed")
    keys = [e.dedup_key for e in bus.envelopes_of_type("tasks.task_updated")]
    assert keys[0] == keys[1]


# ── router emission: task_moved (#474 / ADR-0038 cross-list seam) ───────────


async def test_moving_a_task_emits_task_moved_not_task_updated() -> None:
    local = await _local_provider()
    # A second LocalTasksProvider, registered under "google", stands in for a distinct
    # external account for this test's purposes — the router only cares about the account
    # *key*, not which class backs it (`_provider_for`).
    google = await _local_provider()
    bus = _RecordingBus()
    prefs = CollectionPrefs(enabled=[CollectionRef(account="google", collection="work")])
    router = TasksRouter(
        local=local,
        external={"google": google},
        prefs=_StaticPrefs(prefs),
        bus=bus,  # type: ignore[arg-type]
    )
    created = await local.add_task(TENANT, "Ship PR")  # seeded directly, bypassing the router
    bus.published.clear()

    moved = await router.update_task(TENANT, created.id, to_list_id="work")

    assert bus.envelopes_of_type("tasks.task_updated") == []
    [envelope] = bus.envelopes_of_type("tasks.task_moved")
    assert envelope.dedup_key == f"google:{moved.id}"
    assert envelope.payload["to_list"] == "work"
    assert envelope.payload["from_list"] == "Personal"


# ── scheduler: task_due_soon / task_overdue (#664) ───────────────────────────


async def _today_str(offset_days: int = 0) -> str:
    return (date.today() + timedelta(days=offset_days)).isoformat()


async def _scheduler_fixtures() -> tuple[TasksRouter, LeadTimePrefsStore, FiredMarkerStore]:
    local = await _local_provider()
    router = TasksRouter(local=local, external={}, prefs=_StaticPrefs())  # type: ignore[arg-type]
    lead_prefs = LeadTimePrefsStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await lead_prefs.init()
    markers = FiredMarkerStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await markers.init()
    return router, lead_prefs, markers


async def test_due_soon_fires_for_a_task_due_tomorrow_with_the_default_lead() -> None:
    router, lead_prefs, markers = await _scheduler_fixtures()
    task = await router.add_task(TENANT, "Renew passport", due=await _today_str(1))
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=router,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        today=await _today_str(),  # type: ignore[arg-type]
    )
    [envelope] = bus.envelopes_of_type("tasks.task_due_soon")
    assert envelope.dedup_key == f"local:{task.id}:due_soon"
    assert envelope.payload["lead_days"] == 1


async def test_due_soon_does_not_fire_for_a_task_due_far_in_the_future() -> None:
    router, lead_prefs, markers = await _scheduler_fixtures()
    await router.add_task(TENANT, "Someday", due=await _today_str(30))
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=router,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        today=await _today_str(),  # type: ignore[arg-type]
    )
    assert bus.envelopes_of_type("tasks.task_due_soon") == []


async def test_due_soon_fires_only_once_across_ticks() -> None:
    router, lead_prefs, markers = await _scheduler_fixtures()
    await router.add_task(TENANT, "Renew passport", due=await _today_str(1))
    bus = _RecordingBus()
    for _ in range(3):
        await tick(
            tenant=TENANT,
            provider=router,
            lead_prefs=lead_prefs,
            markers=markers,
            bus=bus,
            today=await _today_str(),  # type: ignore[arg-type]
        )
    assert len(bus.envelopes_of_type("tasks.task_due_soon")) == 1


async def test_due_soon_survives_a_process_restart() -> None:
    marker_engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    first = FiredMarkerStore(marker_engine)
    await first.init()
    assert await first.try_claim(tenant=TENANT, task_id="local:t1", marker="due_soon")

    reopened = FiredMarkerStore(marker_engine)  # a fresh instance, simulating a process restart
    assert await reopened.has_fired(tenant=TENANT, task_id="local:t1", marker="due_soon")
    assert not await reopened.try_claim(tenant=TENANT, task_id="local:t1", marker="due_soon")


async def test_due_soon_honors_a_custom_lead() -> None:
    router, lead_prefs, markers = await _scheduler_fixtures()
    await lead_prefs.set_lead_days(TENANT, 7)
    await router.add_task(TENANT, "Renew passport", due=await _today_str(5))
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=router,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        today=await _today_str(),  # type: ignore[arg-type]
    )
    [envelope] = bus.envelopes_of_type("tasks.task_due_soon")
    assert envelope.payload["lead_days"] == 7


async def test_overdue_fires_for_a_task_due_yesterday() -> None:
    router, lead_prefs, markers = await _scheduler_fixtures()
    task = await router.add_task(TENANT, "Overdue thing", due=await _today_str(-1))
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=router,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        today=await _today_str(),  # type: ignore[arg-type]
    )
    [envelope] = bus.envelopes_of_type("tasks.task_overdue")
    assert envelope.dedup_key == f"local:{task.id}:overdue"


async def test_a_completed_task_never_fires_due_soon_or_overdue() -> None:
    router, lead_prefs, markers = await _scheduler_fixtures()
    task = await router.add_task(TENANT, "Done already", due=await _today_str(-1))
    await router.complete_task(TENANT, task.id)
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=router,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        today=await _today_str(),  # type: ignore[arg-type]
    )
    assert bus.envelopes_of_type("tasks.task_overdue") == []
    assert bus.envelopes_of_type("tasks.task_due_soon") == []


async def test_a_task_with_no_due_date_never_fires() -> None:
    router, lead_prefs, markers = await _scheduler_fixtures()
    await router.add_task(TENANT, "Whenever")
    bus = _RecordingBus()
    await tick(
        tenant=TENANT,
        provider=router,
        lead_prefs=lead_prefs,
        markers=markers,
        bus=bus,
        today=await _today_str(),  # type: ignore[arg-type]
    )
    assert bus.envelopes_of_type("tasks.task_due_soon") == []
    assert bus.envelopes_of_type("tasks.task_overdue") == []
