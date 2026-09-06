"""Tenant data portability for tasks (#871): export/import round-trip + the ADR-0133 rules.

File-backed SQLite per test (AGENTS.md's #677 pitfall) even though nothing here runs a
background task concurrently with the store — matching the module's own house style
(`test_router_collection_resolution.py`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import PortabilityRecord, add_portability_routes, schema_verdict
from epicurus_tasks.db import RepeatStore, TaskStore
from epicurus_tasks.lead_time_prefs import LeadTimePrefsStore
from epicurus_tasks.portability import (
    LEAD_TIME_PREFS_KIND,
    TASK_KIND,
    TASK_REPEAT_KIND,
    TasksPortability,
    repeat_record_id,
)

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


@pytest.fixture()
async def stores(tmp_path: Path) -> AsyncIterator[TasksPortability]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    tasks = TaskStore(engine)
    repeats = RepeatStore(engine)
    lead_prefs = LeadTimePrefsStore(engine)
    await tasks.init()
    await lead_prefs.init()
    yield TasksPortability(tasks=tasks, repeats=repeats, lead_prefs=lead_prefs)
    await engine.dispose()


async def _collect(store: TasksPortability, *, tenant_id: str) -> list[PortabilityRecord]:
    return [record async for record in store.export(tenant_id=tenant_id)]


async def _records(records: list[PortabilityRecord]) -> AsyncIterator[PortabilityRecord]:
    for record in records:
        yield record


async def _seed(store: TasksPortability, *, tenant_id: str) -> None:
    """A local task with a repeat rule, a completed task, and a due task with lead-time
    prefs — plus an external (Google-style) repeat rule, matching the lane brief's scenario."""
    await store._tasks.add_task(
        tenant_id=tenant_id,
        title="Water the plants",
        notes="every week",
        due="2026-09-10",
        status="open",
        priority="low",
        tags=["home"],
        repeat="FREQ=WEEKLY",
    )
    done = await store._tasks.add_task(
        tenant_id=tenant_id,
        title="Ship the report",
        notes=None,
        due="2026-09-01",
        status="done",
        priority="high",
        tags=[],
    )
    await store._tasks.update_task(tenant_id=tenant_id, task_id=done.id, status="done")
    await store._tasks.add_task(
        tenant_id=tenant_id,
        title="Renew passport",
        notes=None,
        due="2026-10-01",
        status="open",
    )
    await store._repeats.set(
        tenant_id=tenant_id, list_id="@default", task_id="g-task-1", rrule="FREQ=MONTHLY"
    )
    await store._lead_prefs.set_lead_days(tenant_id, 3)


def test_schema_is_tasks_v1(stores: TasksPortability) -> None:
    assert stores.schema == "tasks/1"
    assert schema_verdict("tasks/1", stores.schema) == "ok"
    assert schema_verdict("tasks/2", stores.schema) == "newer"
    assert schema_verdict("tasks/0", stores.schema) == "older"
    assert schema_verdict("calendar/1", stores.schema) == "foreign"


async def test_export_shape(stores: TasksPortability) -> None:
    await _seed(stores, tenant_id=TENANT)
    records = await _collect(stores, tenant_id=TENANT)
    kinds = {r.kind for r in records}
    assert kinds == {TASK_KIND, TASK_REPEAT_KIND, LEAD_TIME_PREFS_KIND}

    tasks = [r for r in records if r.kind == TASK_KIND]
    assert len(tasks) == 3
    for record in tasks:
        assert "tenant_id" not in record.data
        assert "pk" not in record.data
        assert record.id  # the task's own uuid, not a surrogate pk

    repeat = next(r for r in records if r.kind == TASK_REPEAT_KIND)
    assert repeat.id == repeat_record_id("@default", "g-task-1")
    assert repeat.data == {"list_id": "@default", "task_id": "g-task-1", "rrule": "FREQ=MONTHLY"}

    prefs = next(r for r in records if r.kind == LEAD_TIME_PREFS_KIND)
    assert prefs.data == {"lead_days": 3}


async def test_export_omits_unset_lead_time_prefs(stores: TasksPortability) -> None:
    """No explicit lead-time preference set → no `lead_time_prefs` record at all — the
    module default travels with the code, not the archive."""
    await stores._tasks.add_task(
        tenant_id=TENANT, title="Solo task", notes=None, due=None, status="open"
    )
    records = await _collect(stores, tenant_id=TENANT)
    assert LEAD_TIME_PREFS_KIND not in {r.kind for r in records}


async def test_round_trip(stores: TasksPortability) -> None:
    """Export → wipe → import → the store looks the same as before the wipe."""
    await _seed(stores, tenant_id=TENANT)
    exported = await _collect(stores, tenant_id=TENANT)
    before = await stores._tasks.list_tasks(tenant_id=TENANT, scope="all")

    for task in before:
        await stores._tasks.delete_task(tenant_id=TENANT, task_id=task.id)
    await stores._repeats.delete(tenant_id=TENANT, list_id="@default", task_id="g-task-1")
    assert await stores._tasks.list_tasks(tenant_id=TENANT, scope="all") == []
    assert (
        await stores._repeats.get(tenant_id=TENANT, list_id="@default", task_id="g-task-1") is None
    )

    report = await stores.import_(tenant_id=TENANT, records=_records(exported), dry_run=False)
    assert report.counts[TASK_KIND].created == 3
    assert report.counts[TASK_REPEAT_KIND].created == 1
    # lead-time prefs already existed (set by `_seed`) and is untouched by the wipe above —
    # an import of the same value over an existing row is correctly "updated", not "created".
    assert report.counts[LEAD_TIME_PREFS_KIND].updated == 1
    assert report.warnings == []

    after = await stores._tasks.list_tasks(tenant_id=TENANT, scope="all")
    assert {t.id for t in after} == {t.id for t in before}
    by_id = {t.id: t for t in after}
    for task in before:
        restored = by_id[task.id]
        assert restored.title == task.title
        assert restored.notes == task.notes
        assert restored.due == task.due
        assert restored.status == task.status
        assert restored.priority == task.priority
        assert restored.tags == task.tags
        assert restored.repeat == task.repeat
        assert restored.completed_at == task.completed_at

    assert await stores._repeats.get(tenant_id=TENANT, list_id="@default", task_id="g-task-1") == (
        "FREQ=MONTHLY"
    )
    assert await stores._lead_prefs.get_lead_days(TENANT) == 3


async def test_second_apply_is_a_no_op(stores: TasksPortability) -> None:
    await _seed(stores, tenant_id=TENANT)
    exported = await _collect(stores, tenant_id=TENANT)

    first = await stores.import_(tenant_id=TENANT, records=_records(exported), dry_run=False)
    assert first.total == len(exported)

    second = await stores.import_(tenant_id=TENANT, records=_records(exported), dry_run=False)
    assert second.counts[TASK_KIND].created == 0
    assert second.counts[TASK_KIND].updated == 3
    assert second.counts[TASK_REPEAT_KIND].updated == 1
    assert second.counts[LEAD_TIME_PREFS_KIND].updated == 1

    # Nothing duplicated.
    assert len(await stores._tasks.list_tasks(tenant_id=TENANT, scope="all")) == 3


async def test_dry_run_writes_nothing(stores: TasksPortability) -> None:
    await _seed(stores, tenant_id=TENANT)
    exported = await _collect(stores, tenant_id=TENANT)

    # Wipe, then a dry-run import must count without writing.
    for task in await stores._tasks.list_tasks(tenant_id=TENANT, scope="all"):
        await stores._tasks.delete_task(tenant_id=TENANT, task_id=task.id)
    await stores._repeats.delete(tenant_id=TENANT, list_id="@default", task_id="g-task-1")

    # Move the lead time away from the exported value, so a write during the dry run would
    # be visible rather than hidden behind a coincidence.
    await stores._lead_prefs.set_lead_days(TENANT, 9)

    report = await stores.import_(tenant_id=TENANT, records=_records(exported), dry_run=True)
    assert report.counts[TASK_KIND].created == 3
    assert report.counts[TASK_REPEAT_KIND].created == 1
    assert report.counts[LEAD_TIME_PREFS_KIND].updated == 1

    assert await stores._tasks.list_tasks(tenant_id=TENANT, scope="all") == []
    assert (
        await stores._repeats.get(tenant_id=TENANT, list_id="@default", task_id="g-task-1") is None
    )
    assert await stores._lead_prefs.get_lead_days(TENANT) == 9


async def test_unknown_kind_is_skipped_with_a_warning(stores: TasksPortability) -> None:
    stream = _records([PortabilityRecord(kind="mystery", id="x", data={})])
    report = await stores.import_(tenant_id=TENANT, records=stream, dry_run=False)
    assert report.counts["mystery"].skipped == 1
    assert any("mystery" in w for w in report.warnings)


async def test_tenant_isolation(stores: TasksPortability) -> None:
    await _seed(stores, tenant_id=TENANT)
    exported = await _collect(stores, tenant_id=TENANT)

    report = await stores.import_(tenant_id=OTHER_TENANT, records=_records(exported), dry_run=False)
    assert report.counts[TASK_KIND].created == 3

    tenant_a = await stores._tasks.list_tasks(tenant_id=TENANT, scope="all")
    tenant_b = await stores._tasks.list_tasks(tenant_id=OTHER_TENANT, scope="all")
    assert len(tenant_a) == 3
    assert len(tenant_b) == 3
    # Same stable ids, but two distinct tenant-scoped rows underneath.
    assert {t.id for t in tenant_a} == {t.id for t in tenant_b}

    # tenant_id never travels in the record itself — the same exported ids come back
    # whichever tenant is asked to export next.
    reexported = await _collect(stores, tenant_id=OTHER_TENANT)
    assert {r.id for r in reexported if r.kind == TASK_KIND} == {
        r.id for r in exported if r.kind == TASK_KIND
    }


async def test_a_malformed_record_is_skipped_and_named_not_raised(
    stores: TasksPortability,
) -> None:
    """A line this module cannot read must not undo the lines before it.

    The three kinds commit through three separate stores, so no transaction spans the stream:
    raising would answer 400 while leaving everything ahead of the bad line already written.
    A bad record is therefore counted `skipped` and named, and the good ones still land.
    """
    stream = _records(
        [
            PortabilityRecord(kind=TASK_KIND, id="t-good", data={"title": "Buy milk"}),
            PortabilityRecord(kind=TASK_REPEAT_KIND, id="broken", data={"list_id": "@default"}),
            PortabilityRecord(kind=LEAD_TIME_PREFS_KIND, id="prefs", data={"lead_days": "three"}),
            PortabilityRecord(kind=TASK_KIND, id="t-after", data={"title": "Call the vet"}),
        ]
    )
    report = await stores.import_(tenant_id=TENANT, records=stream, dry_run=False)

    assert report.counts[TASK_KIND].created == 2
    assert report.counts[TASK_REPEAT_KIND].skipped == 1
    assert report.counts[LEAD_TIME_PREFS_KIND].skipped == 1
    assert any("broken" in w for w in report.warnings)
    assert any("prefs" in w for w in report.warnings)

    # Both good records landed — the one before the bad line and the one after it.
    titles = {t.title for t in await stores._tasks.list_tasks(tenant_id=TENANT, scope="all")}
    assert titles == {"Buy milk", "Call the vet"}
    # And `lead_days: "three"` did not become a lead time by accident.
    assert await stores._lead_prefs.export_pref(TENANT) is None


async def test_a_boolean_lead_days_is_not_a_lead_time_of_one(stores: TasksPortability) -> None:
    """`bool` is an `int` in Python; `lead_days: true` must not import as one day."""
    stream = _records(
        [PortabilityRecord(kind=LEAD_TIME_PREFS_KIND, id="prefs", data={"lead_days": True})]
    )
    report = await stores.import_(tenant_id=TENANT, records=stream, dry_run=False)

    assert report.counts[LEAD_TIME_PREFS_KIND].skipped == 1
    assert await stores._lead_prefs.export_pref(TENANT) is None


async def test_an_unreadable_created_at_is_named_rather_than_silently_replaced(
    stores: TasksPortability,
) -> None:
    """The row still lands, but the operator is told its date was dropped."""
    stream = _records(
        [PortabilityRecord(kind=TASK_KIND, id="t-1", data={"title": "x", "created_at": "nonsense"})]
    )
    report = await stores.import_(tenant_id=TENANT, records=stream, dry_run=False)

    assert report.counts[TASK_KIND].created == 1
    assert any("created_at" in w for w in report.warnings)


async def test_an_existing_repeat_rule_is_updated_in_place_never_deleted(
    stores: TasksPortability,
) -> None:
    """ADR-0133's "never deletes", read straight off the import path.

    The module's own edit path replaces a rule by deleting and re-inserting it; the import
    path must not, so the row keeps its identity across an apply.
    """
    await stores._repeats.set(
        tenant_id=TENANT, list_id="@default", task_id="g-task-1", rrule="FREQ=WEEKLY"
    )
    before = await _repeat_pk(stores)

    outcome = await stores._repeats.upsert(
        tenant_id=TENANT, list_id="@default", task_id="g-task-1", rrule="FREQ=MONTHLY"
    )

    assert outcome == "updated"
    assert await _repeat_pk(stores) == before
    assert (
        await stores._repeats.get(tenant_id=TENANT, list_id="@default", task_id="g-task-1")
        == "FREQ=MONTHLY"
    )


async def _repeat_pk(stores: TasksPortability) -> int:
    """The surrogate key of the seeded repeat rule — unchanged means "not re-inserted"."""
    from sqlalchemy import select

    from epicurus_tasks.db import _StoredRepeat

    async with stores._repeats._session() as session:
        pk = await session.scalar(
            select(_StoredRepeat.pk).where(
                _StoredRepeat.tenant_id == TENANT,
                _StoredRepeat.list_id == "@default",
                _StoredRepeat.task_id == "g-task-1",
            )
        )
    assert pk is not None
    return int(pk)


async def test_older_and_newer_schema_verdicts_via_the_route(tmp_path: Path) -> None:
    """`add_portability_routes`'s compatibility gate: same/older/newer/foreign (ADR-0133).

    File-backed SQLite and an in-process ASGI client rather than `:memory:` + `TestClient`:
    the sync client drives the app on a loop of its own, and an in-memory database is one
    shared `StaticPool` connection across both — the #677 failure mode, one record away.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from epicurus_core import EpicurusModule

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'routes.db'}")
    tasks = TaskStore(engine)
    repeats = RepeatStore(engine)
    lead_prefs = LeadTimePrefsStore(engine)
    await tasks.init()
    await lead_prefs.init()
    store = TasksPortability(tasks=tasks, repeats=repeats, lead_prefs=lead_prefs)

    module = EpicurusModule("tasks", version="0.24.0", portable=True)
    app = FastAPI()
    add_portability_routes(app, module, store)

    def _stream(schema: str, *lines: str) -> bytes:
        return "".join([f'{{"schema": "{schema}"}}\n', *(f"{line}\n" for line in lines)]).encode()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://tasks") as client:
        older = await client.post(
            "/import", params={"tenant_id": TENANT}, content=_stream("tasks/0")
        )
        assert older.status_code == 200
        assert any("older" in w for w in older.json()["warnings"])

        newer = await client.post(
            "/import", params={"tenant_id": TENANT}, content=_stream("tasks/2")
        )
        assert newer.status_code == 409

        foreign = await client.post(
            "/import", params={"tenant_id": TENANT}, content=_stream("calendar/1")
        )
        assert foreign.status_code == 409

        # A malformed record answers 200 with the bad line named — not a 400 over a stream
        # whose earlier records this module has already committed.
        partial = await client.post(
            "/import",
            params={"tenant_id": TENANT},
            content=_stream(
                "tasks/1",
                '{"kind":"task","id":"t-1","data":{"title":"Buy milk"}}',
                '{"kind":"task_repeat","id":"broken","data":{}}',
            ),
        )
        assert partial.status_code == 200
        body = partial.json()
        assert body["counts"]["task"]["created"] == 1
        assert body["counts"]["task_repeat"]["skipped"] == 1
        assert any("broken" in w for w in body["warnings"])

    await engine.dispose()
