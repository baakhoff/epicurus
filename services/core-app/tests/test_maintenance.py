"""Unit tests for the maintenance orchestrator (ADR-0060): registry, run, scope, containment."""

from __future__ import annotations

import asyncio
from typing import Any

from epicurus_core_app.maintenance import (
    JobStatus,
    MaintenanceJob,
    MaintenanceOrchestrator,
    extraction_drain_job,
    module_reindex_job,
)

TENANT = "local"


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
    jobs: list[MaintenanceJob], *, bus: _FakeBus | None = None, **kw: Any
) -> MaintenanceOrchestrator:
    return MaintenanceOrchestrator(
        jobs,
        bus=bus or _FakeBus(),  # type: ignore[arg-type]
        default_tenant=TENANT,
        timezone=_tz,
        **kw,
    )


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


async def test_schedule_metadata() -> None:
    orch = _orch([_job("a")], hour=5, schedule_enabled=True)
    assert orch.schedule_enabled is True and orch.schedule_hour == 5


async def test_run_periodic_is_a_noop_when_disabled() -> None:
    orch = _orch([_job("a")], schedule_enabled=False)
    # Returns immediately rather than entering the sleep loop; wrap to fail loudly on a hang.
    await asyncio.wait_for(orch.run_periodic(), timeout=2)


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
