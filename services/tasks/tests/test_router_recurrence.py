"""Router-orchestrated recurrence materialization (ADR-0082).

The router owns the "complete a recurring task → spawn its next instance" logic, provider-
agnostically. These tests exercise it end-to-end against the *real* local store (no mocking) with
an injected clock, which is the clearest way to prove the behavior: completing a recurring task
creates exactly one successor with the right due date, retires the rule on the completed instance
(so re-completing can't double-fire), and stops cleanly when the series is exhausted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import CollectionPrefs
from epicurus_tasks.db import TaskStore
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.models import Task
from epicurus_tasks.router import TasksRouter, _resolve_timezone, operator_clock

TENANT = "t"
MON = "2026-07-06"
THU = "2026-07-09"
FRI = "2026-07-10"
NEXT_MON = "2026-07-13"


def _fixed(date: str) -> Callable[[], Awaitable[str]]:
    """A `now` clock pinned to *date*, bypassing timezone resolution entirely (#535) — these
    tests pin recurrence math to an exact day, independent of wall-clock or operator zone."""

    async def _today() -> str:
        return date

    return _today


class _AddFailsProvider:
    """Wraps a real provider; ``add_task`` always raises, everything else delegates (#515)."""

    def __init__(self, inner: LocalTasksProvider) -> None:
        self._inner = inner
        self.add_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def add_task(self, *args: Any, **kwargs: Any) -> Task:
        self.add_calls += 1
        raise RuntimeError("boom: provider unavailable")


class _RetireFailsProvider:
    """Wraps a real provider; ``update_task`` always raises, everything else delegates (#515)."""

    def __init__(self, inner: LocalTasksProvider) -> None:
        self._inner = inner
        self.update_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def update_task(self, *args: Any, **kwargs: Any) -> Task:
        self.update_calls += 1
        raise RuntimeError("boom: provider unavailable")


class _LocalPrefs:
    """Prefs source with nothing enabled → the router routes everything to the local store."""

    async def get_collections(self) -> CollectionPrefs:
        return CollectionPrefs()


def _router(store: TaskStore, *, today: str) -> TasksRouter:
    return TasksRouter(
        local=LocalTasksProvider(store),
        external={},
        prefs=_LocalPrefs(),
        now=_fixed(today),
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


# ── materialize failure paths (#515) ──────────────────────────────────────────


async def test_add_failure_leaves_completion_intact_and_rule_live(store: TaskStore) -> None:
    # If spawning the successor fails, the completion itself must still succeed — and the
    # rule stays live on the completed task (not retired) so a retry can pick it up later,
    # rather than silently losing the recurrence.
    inner = LocalTasksProvider(store)
    task = await inner.add_task(TENANT, "Water plants", due=MON, repeat="FREQ=WEEKLY")
    flaky = _AddFailsProvider(inner)
    router = TasksRouter(local=flaky, external={}, prefs=_LocalPrefs(), now=_fixed(MON))

    done = await router.complete_task(TENANT, task.id)  # must not raise
    assert done.completed
    assert flaky.add_calls == 1

    open_tasks, done_tasks = _partition(await inner.list_tasks(TENANT, scope="all"))
    assert open_tasks == []  # no successor was created
    assert done_tasks[0].repeat == "FREQ=WEEKLY"  # rule left live, not retired


async def test_retire_failure_after_successful_add_is_logged_not_raised(store: TaskStore) -> None:
    # The dangerous case: the successor already exists when the retire write fails. The
    # completion must still succeed, and the retire is retried once before being logged and
    # given up on — never raised back to the caller.
    inner = LocalTasksProvider(store)
    task = await inner.add_task(TENANT, "Water plants", due=MON, repeat="FREQ=WEEKLY")
    flaky = _RetireFailsProvider(inner)
    router = TasksRouter(local=flaky, external={}, prefs=_LocalPrefs(), now=_fixed(MON))

    done = await router.complete_task(TENANT, task.id)  # must not raise
    assert done.completed
    assert flaky.update_calls == 2  # the initial attempt plus one retry

    open_tasks, _ = _partition(await inner.list_tasks(TENANT, scope="all"))
    assert len(open_tasks) == 1  # the successor exists even though the retire never landed
    assert open_tasks[0].due == NEXT_MON


# ── overdue sweep (#515) ───────────────────────────────────────────────────────


async def test_list_tasks_materializes_an_overdue_recurring_task(store: TaskStore) -> None:
    # A recurring task nobody completed, now overdue, gets a fresh successor on read — the
    # series doesn't stall forever waiting for a completion that may never come.
    router = _router(store, today=THU)
    await router.add_task(TENANT, "Water plants", due=MON, repeat="FREQ=WEEKLY")

    open_tasks, _ = _partition(await router.list_tasks(TENANT, scope="all"))
    assert len(open_tasks) == 2
    original = next(t for t in open_tasks if t.due == MON)
    successor = next(t for t in open_tasks if t.due != MON)
    # The overdue original stays open (the operator's call whether to still do it) but its
    # rule is retired so it is never swept again.
    assert original.repeat is None
    # The successor's due is skip-missed-advanced from *today*, exactly like a late completion.
    assert successor.due == NEXT_MON
    assert successor.repeat == "FREQ=WEEKLY"


async def test_list_tasks_leaves_a_not_yet_due_recurring_task_alone(store: TaskStore) -> None:
    router = _router(store, today=MON)
    await router.add_task(TENANT, "Water plants", due=NEXT_MON, repeat="FREQ=WEEKLY")
    open_tasks, _ = _partition(await router.list_tasks(TENANT, scope="all"))
    assert len(open_tasks) == 1  # not overdue yet — no sweep, no successor
    assert open_tasks[0].repeat == "FREQ=WEEKLY"


async def test_list_tasks_leaves_an_overdue_non_recurring_task_alone(store: TaskStore) -> None:
    router = _router(store, today=THU)
    await router.add_task(TENANT, "Plain overdue task", due=MON)
    open_tasks, _ = _partition(await router.list_tasks(TENANT, scope="all"))
    assert len(open_tasks) == 1  # nothing to sweep — no repeat rule to advance


# ── sweep hardening: concurrent-read race + retire-failure amplification (#533) ─────────


async def test_sweep_with_persistent_retire_failure_creates_exactly_one_successor_across_reads(
    store: TaskStore,
) -> None:
    """The sweep's own honest-read behaviour when retire fails, exercised through
    ``list_tasks`` rather than ``complete_task`` (the #517/#528 review noted only the
    completion path had this coverage) — plus the second-read idempotency assertion: without
    the claim guard, a persistently failing retire would spawn a fresh duplicate on *every*
    read since the anchor's rule never actually clears. This proves exactly one successor
    exists after two reads, not two (#533b)."""
    inner = LocalTasksProvider(store)
    await inner.add_task(TENANT, "Water plants", due=MON, repeat="FREQ=WEEKLY")
    flaky = _RetireFailsProvider(inner)
    router = TasksRouter(local=flaky, external={}, prefs=_LocalPrefs(), now=_fixed(THU))

    # First read: the sweep fires, add succeeds, retire fails (and is retried once, still
    # fails) — the read must stay honest about what actually landed.
    first_open, _ = _partition(await router.list_tasks(TENANT, scope="all"))
    assert len(first_open) == 2
    original = next(t for t in first_open if t.due == MON)
    assert original.repeat == "FREQ=WEEKLY"  # retire never landed — stayed live, honestly
    assert flaky.update_calls == 2  # the initial attempt plus one retry

    # Second read: the anchor is still overdue and its rule is still (truthfully) live, so
    # without the claim guard this would materialize *again*. It must not.
    second_open, _ = _partition(await router.list_tasks(TENANT, scope="all"))
    assert len(second_open) == 2  # still just the one successor — no amplification
    successors = [t for t in second_open if t.due == NEXT_MON]
    assert len(successors) == 1


async def test_concurrent_sweeps_of_the_same_overdue_anchor_create_one_successor(
    store: TaskStore,
) -> None:
    """Two 'simultaneous' list_tasks calls (#533a) — e.g. the board and a chat turn — must
    not both materialize the same overdue anchor. ``asyncio.gather`` starts both coroutines
    before either completes and the local store's real async I/O gives them genuine
    interleaving points, so this exercises the actual race window rather than a sequential
    stand-in for it."""
    router = _router(store, today=THU)
    await router.add_task(TENANT, "Water plants", due=MON, repeat="FREQ=WEEKLY")

    await asyncio.gather(
        router.list_tasks(TENANT, scope="all"),
        router.list_tasks(TENANT, scope="all"),
    )

    # Ground truth, read once more after both concurrent calls have settled.
    final_open, _ = _partition(await router.list_tasks(TENANT, scope="all"))
    successors = [t for t in final_open if t.due == NEXT_MON]
    assert len(successors) == 1  # exactly one successor, not two, despite the race


# ── operator-timezone recurrence clock (#535) ──────────────────────────────────


def _tz(name: str) -> Callable[[], Any]:
    """A TimezoneSource stub returning a fixed IANA zone name (mirrors calendar's #433 tests)."""

    async def source() -> str:
        return name

    return source


async def test_resolve_timezone_with_no_source_defaults_to_utc() -> None:
    assert await _resolve_timezone(None) is UTC


async def test_resolve_timezone_resolves_the_operator_zone() -> None:
    assert await _resolve_timezone(_tz("America/New_York")) == ZoneInfo("America/New_York")


async def test_resolve_timezone_degrades_to_utc_on_unknown_zone() -> None:
    assert await _resolve_timezone(_tz("Neverland/Nowhere")) is UTC


async def test_resolve_timezone_degrades_to_utc_on_source_error() -> None:
    async def broken() -> str:
        raise RuntimeError("core down")

    assert await _resolve_timezone(broken) is UTC


async def test_operator_clock_reads_today_in_the_resolved_zone() -> None:
    # Proves the wiring end to end: the clock actually reads the *resolved* zone rather than
    # silently staying UTC — the exact regression #535 exists to prevent. A UTC-negative zone
    # (e.g. US Pacific) can disagree with UTC about what day it is right now; comparing against
    # the same `datetime.now(tz)` expression computed here (not a hardcoded UTC one) is what
    # keeps this deterministic without freezing the wall clock.
    tz = ZoneInfo("Pacific/Kiritimati")  # UTC+14 — the furthest-ahead named zone
    clock = operator_clock(_tz("Pacific/Kiritimati"))
    assert await clock() == datetime.now(tz).date().isoformat()
