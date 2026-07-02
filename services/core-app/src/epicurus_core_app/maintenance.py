"""The maintenance orchestrator — one coordinated batch across the core's background jobs.

Background work (memory fact extraction, module re-index/re-embed) runs per-runner on its own
schedule today (ADR-0051, #332). This gives the operator **one trigger** that fans those jobs out
as a single coordinated batch — run now from the UI, or on an opt-in nightly schedule — with a
per-job result and a tenant-scoped ``maintenance.completed`` event (ADR-0060).

The design is a small **registry**: a :class:`MaintenanceJob` is a labelled async unit of work, and
new job types register by being added to the list the orchestrator is built with — no change to the
run/route/schedule machinery. Each job is **contained**: one job's failure is captured as an
``error`` result and never aborts the rest. Jobs run **sequenced** (one at a time) so a batch stays
gentle on a single local GPU, mirroring the nightly extraction drain.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from epicurus_core import EventBus, get_logger
from epicurus_core_app.scheduling import TimezoneProvider, sleep_until_hour

log = get_logger("epicurus_core_app.maintenance")

# Base subject (tenant-scoped at publish time, constraint #1) announced after a batch completes.
MAINTENANCE_COMPLETED_SUBJECT = "maintenance.completed"

JobStatus = Literal["ok", "skipped", "error"]
JobRunner = Callable[[], Awaitable[tuple[JobStatus, str]]]


@dataclass(frozen=True)
class MaintenanceJob:
    """One registered unit of maintenance work.

    ``run`` returns ``(status, detail)`` — ``detail`` is a short human line for the UI. A job that
    raises is reported as ``error`` (its exception text becomes the detail). ``nightly`` marks a job
    light/idempotent enough for the scheduled batch; a heavy job (a full re-embed) sets it ``False``
    so the schedule skips it while the manual "run everything" trigger still includes it.
    """

    key: str
    label: str
    run: JobRunner
    nightly: bool = True


@dataclass
class MaintenanceJobResult:
    """The outcome of one job in a run."""

    key: str
    label: str
    status: JobStatus
    detail: str


@dataclass
class MaintenanceRun:
    """The aggregate result of one maintenance batch."""

    ran_at: str
    scope: Literal["all", "nightly"]
    jobs: list[MaintenanceJobResult] = field(default_factory=list)


class MaintenanceOrchestrator:
    """Runs the registered maintenance jobs as one coordinated, sequenced batch.

    Construction takes the job list (the registry) plus the event bus and schedule config. The
    manual trigger runs **every** job (``scope="all"``); the opt-in nightly loop runs only the
    ``nightly`` jobs. Either way a tenant-scoped ``maintenance.completed`` event is published and
    the last run is cached for the UI. The scheduler is **off by default** — the per-runner nightly
    schedules already cover the unattended case; an operator opts into a single coordinated batch
    (consolidating them onto this orchestrator is the follow-up, ADR-0060).
    """

    def __init__(
        self,
        jobs: list[MaintenanceJob],
        *,
        bus: EventBus,
        default_tenant: str,
        timezone: TimezoneProvider,
        hour: int = 4,
        schedule_enabled: bool = False,
    ) -> None:
        self._jobs = list(jobs)
        self._bus = bus
        self._tenant = default_tenant
        self._timezone = timezone
        self._hour = hour % 24
        self._schedule_enabled = schedule_enabled
        self._last_run: MaintenanceRun | None = None

    @property
    def schedule_enabled(self) -> bool:
        return self._schedule_enabled

    @property
    def schedule_hour(self) -> int:
        return self._hour

    def descriptors(self) -> list[dict[str, object]]:
        """The registered jobs as ``{key, label, nightly}`` dicts (for the UI)."""
        return [{"key": j.key, "label": j.label, "nightly": j.nightly} for j in self._jobs]

    def last_run(self) -> MaintenanceRun | None:
        """The most recent run's result, or ``None`` if none has run this process."""
        return self._last_run

    async def run(
        self, *, tenant: str | None = None, scope: Literal["all", "nightly"] = "all"
    ) -> MaintenanceRun:
        """Run the batch and publish ``maintenance.completed`` (tenant-scoped); cache + return it.

        ``scope="nightly"`` runs only the ``nightly`` jobs (the scheduled, light batch); ``"all"``
        runs every registered job (the manual "run everything" trigger). Each job is contained: a
        raise becomes an ``error`` result, never aborting the rest.
        """
        who = tenant or self._tenant
        selected = [j for j in self._jobs if scope == "all" or j.nightly]
        results: list[MaintenanceJobResult] = []
        for job in selected:
            try:
                status, detail = await job.run()
            except Exception as exc:  # one job's failure must never abort the batch
                log.warning("maintenance job failed", job=job.key, error=str(exc))
                status, detail = "error", str(exc)
            results.append(
                MaintenanceJobResult(key=job.key, label=job.label, status=status, detail=detail)
            )
        run = MaintenanceRun(ran_at=datetime.now(UTC).isoformat(), scope=scope, jobs=results)
        self._last_run = run
        try:
            await self._bus.publish(
                MAINTENANCE_COMPLETED_SUBJECT,
                {
                    "ran_at": run.ran_at,
                    "scope": run.scope,
                    "jobs": [
                        {"key": r.key, "status": r.status, "detail": r.detail} for r in results
                    ],
                },
                tenant_id=who,
            )
        except Exception as exc:  # a NATS hiccup must not fail the run the operator just triggered
            log.warning("maintenance event publish failed", error=str(exc))
        log.info(
            "maintenance batch complete",
            scope=scope,
            jobs=len(results),
            errors=sum(1 for r in results if r.status == "error"),
        )
        return run

    async def run_periodic(self) -> None:
        """Loop forever running the nightly batch at the configured hour — a no-op when disabled.

        Each iteration is self-contained: a failed run logs and waits for the next window rather
        than killing the loop. Returns immediately (never loops) when the schedule is off.
        """
        if not self._schedule_enabled:
            return
        while True:
            await sleep_until_hour(self._hour, self._timezone)
            try:
                await self.run(scope="nightly")
            except Exception as exc:  # never let the scheduler die on a transient error
                log.warning("scheduled maintenance run failed", error=str(exc))


# ── Built-in jobs ─────────────────────────────────────────────────────────────


def extraction_drain_job(drain: Callable[[], Awaitable[int]]) -> MaintenanceJob:
    """Drain the deferred fact-extraction queue now (ADR-0051) — light, so nightly-eligible.

    *drain* is :meth:`ExtractionRunner.drain_once`; it already skips when the gateway is paused and
    is best-effort per exchange, so this job only reports how many it processed.
    """

    async def _run() -> tuple[JobStatus, str]:
        count = await drain()
        return "ok", f"distilled {count} pending exchange(s)"

    return MaintenanceJob(
        key="memory-extraction", label="Memory fact extraction", run=_run, nightly=True
    )


def module_reindex_job(reembed: Callable[[], Awaitable[list[dict[str, str]]]]) -> MaintenanceJob:
    """Re-embed every reindexable module (#332) — heavy, so manual-only (``nightly=False``).

    *reembed* is :meth:`ModuleRegistry.reembed`; it returns ``[{module, status}]`` best-effort per
    module. A full re-embed is costly and only needed after the embedding model changes, so it is
    excluded from the scheduled batch and included only in the manual "run everything" trigger.
    """

    async def _run() -> tuple[JobStatus, str]:
        results = await reembed()
        if not results:
            return "skipped", "no reindexable modules"
        started = sum(1 for r in results if r.get("status") == "started")
        failed = [r["module"] for r in results if r.get("status") != "started"]
        detail = f"re-index started on {started}/{len(results)} module(s)"
        if failed:
            detail += f"; failed: {', '.join(failed)}"
        status: JobStatus = "error" if failed and not started else "ok"
        return status, detail

    return MaintenanceJob(
        key="module-reindex", label="Module re-index / re-embed", run=_run, nightly=False
    )


def facts_reembed_job(reembed: Callable[[], Awaitable[int]]) -> MaintenanceJob:
    """Re-embed the tenant's memory facts (#436) — heavy, so manual-only (``nightly=False``).

    *reembed* is a zero-arg closure over :meth:`UserFactStore.reembed_all` for the default
    tenant. Facts aren't a module and don't go through the ``/reindex`` HTTP fan-out above, but
    they're a Qdrant collection just as model-dependent as knowledge/notes, so this folds them
    into the same "Re-embed everything" action (ADR-0054) instead of leaving them out of it.
    """

    async def _run() -> tuple[JobStatus, str]:
        migrated = await reembed()
        return "ok", f"re-embedded {migrated} fact(s)"

    return MaintenanceJob(
        key="facts-reembed", label="Memory facts re-embed", run=_run, nightly=False
    )
