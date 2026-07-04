"""Router-orchestrated recurrence materialization (ADR-0082).

The router owns the "complete a recurring task → spawn its next instance" logic, provider-
agnostically. These tests exercise it end-to-end against the *real* local store (no mocking) with
an injected clock, which is the clearest way to prove the behavior: completing a recurring task
creates exactly one successor with the right due date, retires the rule on the completed instance
(so re-completing can't double-fire), and stops cleanly when the series is exhausted.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import CollectionPrefs
from epicurus_tasks.db import TaskStore
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.models import Task
from epicurus_tasks.router import TasksRouter

TENANT = "t"
MON = "2026-07-06"
THU = "2026-07-09"
FRI = "2026-07-10"
NEXT_MON = "2026-07-13"


class _LocalPrefs:
    """Prefs source with nothing enabled → the router routes everything to the local store."""

    async def get_collections(self) -> CollectionPrefs:
        return CollectionPrefs()


def _router(store: TaskStore, *, today: str) -> TasksRouter:
    return TasksRouter(
        local=LocalTasksProvider(store),
        external={},
        prefs=_LocalPrefs(),
        now=lambda: today,
    )


@pytest.fixture()
async def store() -> TaskStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    ts = TaskStore(engine)
    await ts.init()
    return ts


def _partition(tasks: list[Task]) -> tuple[list[Task], list[Task]]:
    """(open, done) split of an all-scope read."""
    return [t for t in tasks if not t.completed], [t for t in tasks if t.completed]


async def test_completing_a_recurring_task_materializes_the_next_instance(store: TaskStore) -> None:
    router = _router(store, today=MON)
    task = await router.add_task(TENANT, "Water plants", due=MON, repeat="FREQ=WEEKLY")

    done = await router.complete_task(TENANT, task.id)
    assert done.completed

    open_tasks, done_tasks = _partition(await router.list_tasks(TENANT, scope="all"))
    # Exactly one successor, open, one week later, carrying the same rule.
    assert len(open_tasks) == 1
    successor = open_tasks[0]
    assert successor.id != task.id
    assert successor.due == NEXT_MON
    assert successor.repeat == "FREQ=WEEKLY"
    assert successor.title == "Water plants"
    # The completed instance keeps its due date but its rule is retired (moved to the successor).
    assert len(done_tasks) == 1
    assert done_tasks[0].id == task.id
    assert done_tasks[0].repeat is None


async def test_late_completion_skips_missed_occurrences(store: TaskStore) -> None:
    # Daily task due Monday, completed Thursday → next instance is Friday, not Tuesday.
    router = _router(store, today=THU)
    task = await router.add_task(TENANT, "Take meds", due=MON, repeat="FREQ=DAILY")
    await router.complete_task(TENANT, task.id)
    open_tasks, _ = _partition(await router.list_tasks(TENANT, scope="all"))
    assert [t.due for t in open_tasks] == [FRI]


async def test_exhausted_series_creates_no_successor(store: TaskStore) -> None:
    router = _router(store, today=MON)
    task = await router.add_task(TENANT, "Once", due=MON, repeat="FREQ=DAILY;COUNT=1")
    await router.complete_task(TENANT, task.id)
    open_tasks, done_tasks = _partition(await router.list_tasks(TENANT, scope="all"))
    assert open_tasks == []  # no next instance — the series ended
    assert done_tasks[0].repeat is None  # and the spent rule is retired


async def test_non_recurring_task_spawns_nothing(store: TaskStore) -> None:
    router = _router(store, today=MON)
    task = await router.add_task(TENANT, "Plain task", due=MON)
    await router.complete_task(TENANT, task.id)
    open_tasks, _ = _partition(await router.list_tasks(TENANT, scope="all"))
    assert open_tasks == []


async def test_re_completing_does_not_double_fire(store: TaskStore) -> None:
    # Completing the same (already-done) task twice must not spawn a second successor: the rule
    # was retired on the first completion, so the second is an inert no-op.
    router = _router(store, today=MON)
    task = await router.add_task(TENANT, "Water plants", due=MON, repeat="FREQ=WEEKLY")
    await router.complete_task(TENANT, task.id)
    await router.complete_task(TENANT, task.id)  # again
    open_tasks, _ = _partition(await router.list_tasks(TENANT, scope="all"))
    assert len(open_tasks) == 1  # still just the one successor, not two
