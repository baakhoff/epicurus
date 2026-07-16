"""Unit tests for the maintenance orchestrator (ADR-0060): registry, run, scope, containment.

The crux of the #561 coverage is :func:`test_start_run_is_nonblocking_with_pending_progress`
(a batch outlives the caller) and :func:`test_shutdown_cancels_inflight_and_marks_interrupted`
(a batch is still cleanly interruptible at process shutdown) — the same two properties
``agent/live_runs.py`` established for chat turns (#376), now for maintenance batches.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from epicurus_core_app import maintenance as maintenance_module
from epicurus_core_app.maintenance import (
    JobStatus,
    MaintenanceJob,
    MaintenanceOrchestrator,
    MaintenanceRunConflictError,
    extraction_drain_job,
    facts_reembed_job,
    module_reindex_job,
    playbook_reflection_job,
    profile_synthesis_job,
)
from epicurus_core_app.maintenance_schedule_prefs import MaintenanceSchedule

TENANT = "local"

_DISABLED = MaintenanceSchedule(enabled=False, cadence="daily", hour=4)


class _FakeBus:
    """Captures published events for assertions."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any], str | None]] = []

    async def publish(
        self, subject: str, data: dict[str, Any], tenant_id: str | None = None
    ) -> None:
        self.published.append((subject, data, tenant_id))


async def _tz() -> str:
    return "UTC"


def _job(
    key: str,
    *,
    nightly: bool = True,
    status: JobStatus = "ok",
    detail: str = "done",
    boom: bool = False,
) -> MaintenanceJob:
    async def run() -> tuple[JobStatus, str]:
        if boom:
            raise RuntimeError("kaboom")
        return status, detail

    return MaintenanceJob(key=key, label=key.title(), run=run, nightly=nightly)


def _orch(
    jobs: list[MaintenanceJob],
    *,
    bus: _FakeBus | None = None,
    schedule: MaintenanceSchedule = _DISABLED,
    **kw: Any,
) -> MaintenanceOrchestrator:
    async def _schedule() -> MaintenanceSchedule:
        return schedule

    return MaintenanceOrchestrator(
        jobs,
        bus=bus or _FakeBus(),  # type: ignore[arg-type]
        default_tenant=TENANT,
        timezone=_tz,
        schedule=_schedule,
        **kw,
    )


def _gated_job(key: str, gate: asyncio.Event, *, nightly: bool = True) -> MaintenanceJob:
    """A job that stays ``running`` until *gate* is set — for observing in-flight state."""

    async def run() -> tuple[JobStatus, str]:
        await gate.wait()
        return "ok", f"{key} done"

    return MaintenanceJob(key=key, label=key.title(), run=run, nightly=nightly)


async def _until_idle(orch: MaintenanceOrchestrator, *, timeout: float = 2.0) -> None:
    """Poll ``current_run`` to ``None`` — the public-API way to await a `start_run` completion."""

    async def _poll() -> None:
        while orch.current_run() is not None:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def test_run_all_executes_every_job_and_publishes_tenant_scoped() -> None:
    bus = _FakeBus()
    orch = _orch([_job("a"), _job("b", nightly=False)], bus=bus)
    run = await orch.run(tenant=TENANT)
    assert [r.key for r in run.jobs] == ["a", "b"]
    assert all(r.status == "ok" for r in run.jobs)
    assert run.scope == "all"
    assert orch.last_run() is run
    # exactly one tenant-scoped completion event, carrying the per-job summary
    assert len(bus.published) == 1
    subject, data, tenant_id = bus.published[0]
    assert subject == "maintenance.completed" and tenant_id == TENANT
    assert {j["key"] for j in data["jobs"]} == {"a", "b"}


async def test_nightly_scope_runs_only_nightly_jobs() -> None:
    orch = _orch([_job("light", nightly=True), _job("heavy", nightly=False)])
    run = await orch.run(scope="nightly")
    assert [r.key for r in run.jobs] == ["light"]
    assert run.scope == "nightly"


async def test_a_failing_job_is_contained() -> None:
    orch = _orch([_job("ok1"), _job("bad", boom=True), _job("ok2")])
    run = await orch.run()
    assert {r.key: r.status for r in run.jobs} == {"ok1": "ok", "bad": "error", "ok2": "ok"}
    assert "kaboom" in next(r.detail for r in run.jobs if r.key == "bad")


async def test_publish_failure_does_not_fail_the_run() -> None:
    class _BoomBus(_FakeBus):
        async def publish(
            self, subject: str, data: dict[str, Any], tenant_id: str | None = None
        ) -> None:
            raise RuntimeError("nats down")

    orch = _orch([_job("a")], bus=_BoomBus())
    run = await orch.run()  # must not raise
    assert run.jobs[0].status == "ok"


async def test_descriptors_advertise_jobs() -> None:
    orch = _orch([_job("a"), _job("b", nightly=False)])
    assert orch.descriptors() == [
        {"key": "a", "label": "A", "nightly": True},
        {"key": "b", "label": "B", "nightly": False},
    ]


# ── schedule tick (#621) ───────────────────────────────────────────────────────


@contextlib.contextmanager
def _frozen_at(when: datetime) -> Iterator[None]:
    """Patch ``maintenance.datetime.now()`` to always return *when* (tz-aware, any zone).

    ``_tick`` computes "local now" via ``datetime.now(tz)`` (and stamps ``_last_scheduled_fire``
    the same way) — freezing it is what makes cadence/window tests deterministic without a real
    clock or a real sleep.
    """

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return when if tz is None else when.astimezone(tz)

    real = maintenance_module.datetime
    maintenance_module.datetime = _Frozen  # type: ignore[assignment]
    try:
        yield
    finally:
        maintenance_module.datetime = real


async def test_tick_is_a_noop_when_disabled() -> None:
    orch = _orch([_job("a")], schedule=MaintenanceSchedule(enabled=False, cadence="daily", hour=4))
    with _frozen_at(datetime(2026, 1, 1, 4, 0, tzinfo=UTC)):  # exactly the target hour
        await orch._tick()
    assert orch.last_run() is None


async def test_tick_runs_the_nightly_batch_when_due() -> None:
    orch = _orch(
        [_job("light", nightly=True), _job("heavy", nightly=False)],
        schedule=MaintenanceSchedule(enabled=True, cadence="daily", hour=4),
    )
    with _frozen_at(datetime(2026, 1, 1, 4, 30, tzinfo=UTC)):
        await orch._tick()
    run = orch.last_run()
    assert run is not None
    assert [r.key for r in run.jobs] == ["light"]  # nightly scope — heavy job excluded
    assert run.scope == "nightly"


async def test_tick_is_a_noop_outside_the_scheduled_hour() -> None:
    orch = _orch([_job("a")], schedule=MaintenanceSchedule(enabled=True, cadence="daily", hour=4))
    with _frozen_at(datetime(2026, 1, 1, 9, 0, tzinfo=UTC)):
        await orch._tick()
    assert orch.last_run() is None


async def test_tick_does_not_double_fire_within_the_same_window() -> None:
    orch = _orch([_job("a")], schedule=MaintenanceSchedule(enabled=True, cadence="daily", hour=4))
    with _frozen_at(datetime(2026, 1, 1, 4, 5, tzinfo=UTC)):
        await orch._tick()
    first_run = orch.last_run()
    assert first_run is not None
    with _frozen_at(datetime(2026, 1, 1, 4, 45, tzinfo=UTC)):  # a later poll, same 4am window
        await orch._tick()
    assert orch.last_run() is first_run  # unchanged — no second run this window


async def test_tick_hourly_cadence_fires_once_per_hour() -> None:
    orch = _orch([_job("a")], schedule=MaintenanceSchedule(enabled=True, cadence="hourly", hour=0))
    with _frozen_at(datetime(2026, 1, 1, 9, 5, tzinfo=UTC)):
        await orch._tick()
    first_run = orch.last_run()
    assert first_run is not None
    with _frozen_at(datetime(2026, 1, 1, 9, 50, tzinfo=UTC)):  # still the 9am hour
        await orch._tick()
    assert orch.last_run() is first_run
    with _frozen_at(datetime(2026, 1, 1, 10, 5, tzinfo=UTC)):  # the next hour
        await orch._tick()
    assert orch.last_run() is not first_run


async def test_tick_weekly_cadence_only_fires_on_the_configured_weekday() -> None:
    # 2026-01-01 is a Thursday (weekday()==3); the schedule targets Monday (0).
    orch = _orch(
        [_job("a")],
        schedule=MaintenanceSchedule(enabled=True, cadence="weekly", hour=4, weekday=0),
    )
    with _frozen_at(datetime(2026, 1, 1, 4, 30, tzinfo=UTC)):
        await orch._tick()
    assert orch.last_run() is None  # Thursday — not the configured Monday
    with _frozen_at(datetime(2026, 1, 5, 4, 30, tzinfo=UTC)):  # the following Monday
        await orch._tick()
    assert orch.last_run() is not None


async def test_tick_swallows_conflict_when_a_manual_run_is_in_flight() -> None:
    gate = asyncio.Event()
    orch = _orch(
        [_gated_job("a", gate)],
        schedule=MaintenanceSchedule(enabled=True, cadence="daily", hour=4),
    )
    orch.start_run()  # a manual run already in flight when the nightly window hits
    with _frozen_at(datetime(2026, 1, 1, 4, 0, tzinfo=UTC)):
        await orch._tick()  # must not raise
    assert orch.current_run() is not None  # the original in-flight run, untouched
    gate.set()
    await _until_idle(orch)


# ── in-flight tracking, background start, concurrent-run guard (#561) ──────────


async def test_start_run_is_nonblocking_with_pending_progress() -> None:
    gate = asyncio.Event()
    orch = _orch([_gated_job("a", gate), _job("b")])
    current = orch.start_run()
    # start_run itself never awaits, so the driver hasn't taken its first step at return time.
    assert [p.status for p in current.jobs] == ["pending", "pending"]
    assert orch.current_run() is current
    assert orch.last_run() is None

    gate.set()
    await _until_idle(orch)

    assert orch.current_run() is None
    last = orch.last_run()
    assert last is not None
    assert {r.key: r.status for r in last.jobs} == {"a": "ok", "b": "ok"}


async def test_current_run_shows_the_running_job_live_and_stays_sequenced() -> None:
    gate = asyncio.Event()
    orch = _orch([_gated_job("slow", gate), _job("fast")])
    current = orch.start_run()
    await asyncio.sleep(0)  # let the driver take its first step
    assert current.jobs[0].status == "running"
    assert current.jobs[1].status == "pending"  # sequenced — "fast" hasn't started yet
    gate.set()
    await _until_idle(orch)
    assert [p.status for p in current.jobs] == ["ok", "ok"]


async def test_start_run_conflicts_while_a_batch_is_in_flight() -> None:
    gate = asyncio.Event()
    orch = _orch([_gated_job("a", gate)])
    current = orch.start_run()

    with pytest.raises(MaintenanceRunConflictError) as excinfo:
        orch.start_run()
    assert excinfo.value.current is current

    gate.set()
    await _until_idle(orch)


async def test_run_raises_conflict_while_a_batch_is_in_flight() -> None:
    """The guard `run_periodic` relies on to skip (not double-run) an overlapping nightly window."""
    gate = asyncio.Event()
    orch = _orch([_gated_job("a", gate)])
    current = orch.start_run()

    with pytest.raises(MaintenanceRunConflictError) as excinfo:
        await orch.run()
    assert excinfo.value.current is current

    gate.set()
    await _until_idle(orch)


async def test_shutdown_cancels_inflight_and_marks_interrupted() -> None:
    gate = asyncio.Event()  # never set — the batch is wedged
    orch = _orch([_gated_job("a", gate), _job("b")])
    current = orch.start_run()

    await asyncio.wait_for(orch.shutdown(), timeout=2)

    assert orch.current_run() is None
    assert current.jobs[0].status == "error" and "interrupted" in current.jobs[0].detail
    assert current.jobs[1].status == "error"  # "b" never started; still marked interrupted
    assert orch.last_run() is None  # a cancelled batch is discarded, not published


async def test_shutdown_is_a_noop_when_idle() -> None:
    orch = _orch([_job("a")])
    await asyncio.wait_for(orch.shutdown(), timeout=2)  # must not raise or hang


async def test_shutdown_allows_a_new_run_to_start() -> None:
    gate = asyncio.Event()
    orch = _orch([_gated_job("a", gate)])
    orch.start_run()

    await asyncio.wait_for(orch.shutdown(), timeout=2)

    current2 = orch.start_run()  # the guard must not still think a run is active
    assert current2.jobs[0].status == "pending"
    gate.set()
    await _until_idle(orch)


# ── built-in jobs ─────────────────────────────────────────────────────────────


async def test_extraction_drain_job_reports_count() -> None:
    async def drain() -> int:
        return 7

    job = extraction_drain_job(drain)
    assert job.key == "memory-extraction" and job.nightly is True
    assert await job.run() == ("ok", "distilled 7 pending exchange(s)")


async def test_module_reindex_job_summarizes_partial() -> None:
    async def reembed() -> list[dict[str, str]]:
        return [
            {"module": "knowledge", "status": "started"},
            {"module": "notes", "status": "error"},
        ]

    job = module_reindex_job(reembed)
    assert job.nightly is False
    status, detail = await job.run()
    assert status == "ok" and "1/2" in detail and "notes" in detail


async def test_module_reindex_job_skips_when_empty() -> None:
    async def reembed() -> list[dict[str, str]]:
        return []

    assert await module_reindex_job(reembed).run() == ("skipped", "no reindexable modules")


async def test_module_reindex_job_all_failed_is_error() -> None:
    async def reembed() -> list[dict[str, str]]:
        return [{"module": "k", "status": "error"}]

    status, _ = await module_reindex_job(reembed).run()
    assert status == "error"


async def test_facts_reembed_job_reports_count() -> None:
    async def reembed() -> int:
        return 3

    job = facts_reembed_job(reembed)
    assert job.key == "facts-reembed" and job.nightly is False
    assert await job.run() == ("ok", "re-embedded 3 fact(s)")


async def test_profile_synthesis_job_reports_count() -> None:
    async def synthesize() -> int:
        return 2

    job = profile_synthesis_job(synthesize)
    assert job.key == "memory-profile" and job.nightly is True  # light → runs on the nightly batch
    assert await job.run() == ("ok", "synthesized 2 standing profile(s)")


async def test_playbook_reflection_job_reports_count() -> None:
    async def reflect() -> int:
        return 3

    job = playbook_reflection_job(reflect)
    # Light (one call per active tenant) → the nightly tier, beside extraction and profile.
    assert job.key == "playbook-reflection" and job.nightly is True
    assert await job.run() == ("ok", "staged 3 proposal(s) for review")


async def test_playbook_reflection_job_failure_is_contained() -> None:
    """The registry's containment rule: one job's failure is a result, never an aborted batch."""

    async def reflect() -> int:
        raise RuntimeError("the model exploded")

    run = await _orch([playbook_reflection_job(reflect)]).run()
    assert [j.status for j in run.jobs] == ["error"]
    assert "the model exploded" in run.jobs[0].detail
