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

A batch runs as a **detached background task** (:meth:`MaintenanceOrchestrator.start_run`,
#561), decoupled from whatever HTTP request triggered it — the same shape as chat turns
(``agent/live_runs.py``, #376). ``current_run`` exposes its live per-job progress while it's in
flight; a second start while one is running raises :class:`MaintenanceRunConflictError` so a
caller joins the in-flight run instead of racing a second one.

The *trigger* — enable/disable, cadence (hourly/daily/weekly), time of day, weekday — is a
real, per-tenant, runtime-editable schedule (#621); see ``maintenance_schedule_prefs.py``. This
module only generalizes *when* the nightly batch fires, never *what* it runs: the job registry
above stays a static, additive-only list.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo

from epicurus_core import EventBus, get_logger
from epicurus_core_app.maintenance_schedule_prefs import MaintenanceSchedule, is_due
from epicurus_core_app.scheduling import TimezoneProvider

log = get_logger("epicurus_core_app.maintenance")

# A provider of the tenant's current maintenance schedule (#621) — re-read every tick, so an
# operator's enable/disable or cadence/hour/weekday change (via PUT) takes effect without a
# process restart. Mirrors ``TimezoneProvider``'s zero-arg-async-callable shape.
MaintenanceScheduleProvider = Callable[[], Awaitable[MaintenanceSchedule]]

# Base subject (tenant-scoped at publish time, constraint #1) announced after a batch completes.
MAINTENANCE_COMPLETED_SUBJECT = "maintenance.completed"

JobStatus = Literal["ok", "skipped", "error"]
JobRunner = Callable[[], Awaitable[tuple[JobStatus, str]]]

# A job's live status within an in-flight run. "pending"/"running" are transient — a *completed*
# job (or the terminal :class:`MaintenanceJobResult` in a finished run) is always one of the
# original three.
JobProgressStatus = Literal["pending", "running", "ok", "skipped", "error"]


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
    """The aggregate result of one maintenance batch.

    ``tenant``/``source``/``finished_at`` exist for :attr:`MaintenanceOrchestrator._on_recorded`
    (#733's persisted history) — the in-memory :meth:`MaintenanceOrchestrator.last_run` getter
    that predates them ignores all three, so they default harmlessly for any caller that
    doesn't care.
    """

    ran_at: str
    scope: Literal["all", "nightly"]
    jobs: list[MaintenanceJobResult] = field(default_factory=list)
    tenant: str = ""
    source: Literal["scheduled", "manual"] = "manual"
    finished_at: str = ""


@dataclass
class MaintenanceJobProgress:
    """One job's live status within an in-flight run — mutated in place as the batch sequences."""

    key: str
    label: str
    status: JobProgressStatus = "pending"
    detail: str = ""


@dataclass
class MaintenanceCurrentRun:
    """An in-flight batch's live state — the same object the driver mutates as jobs sequence.

    Polled by ``GET /platform/v1/maintenance`` (as ``current_run``) while non-``None``; the
    Settings card renders per-job progress from it and rehydrates onto it on mount, so a page
    refresh mid-batch lands back on the same live run rather than losing it (#561).
    """

    started_at: str
    scope: Literal["all", "nightly"]
    jobs: list[MaintenanceJobProgress] = field(default_factory=list)


class MaintenanceRunConflictError(RuntimeError):
    """A batch is already in flight — carries it so the caller can join instead of racing a second.

    Raised by :meth:`MaintenanceOrchestrator.start_run` (and thus :meth:`~.run`) when called while
    :attr:`MaintenanceOrchestrator.current_run` is non-``None``. The HTTP layer turns this into a
    409; the nightly scheduler treats it as a benign skip. Mirrors ``RunAlreadyActiveError`` in
    ``agent/live_runs.py`` (#376).
    """

    def __init__(self, current: MaintenanceCurrentRun) -> None:
        super().__init__("a maintenance run is already in progress")
        self.current = current


def _terminal(status: JobProgressStatus) -> JobStatus:
    """A live-progress status, narrowed to the three terminal outcomes `MaintenanceJobResult`
    accepts. By the time `shutdown` calls this, every entry has already been forced out of
    "pending"/"running" — this only satisfies the type checker, which cannot see that."""
    return status if status in ("ok", "skipped", "error") else "error"


class MaintenanceOrchestrator:
    """Runs the registered maintenance jobs as one coordinated, sequenced batch.

    Construction takes the job list (the registry) plus the event bus and a schedule provider.
    The manual trigger runs **every** job (``scope="all"``); the poll-based nightly loop runs
    only the ``nightly`` jobs, and only when the current schedule (read fresh each tick, #621)
    says to. Either way a tenant-scoped ``maintenance.completed`` event is published and the last
    run is cached for the UI. The schedule is **off by default** (an env-configured default,
    operator-overridable at runtime) — the per-runner nightly schedules already cover the
    unattended case; an operator opts into a single coordinated batch (consolidating them onto
    this orchestrator is the follow-up, ADR-0060).
    """

    def __init__(
        self,
        jobs: list[MaintenanceJob],
        *,
        bus: EventBus,
        default_tenant: str,
        timezone: TimezoneProvider,
        schedule: MaintenanceScheduleProvider,
        poll_interval_s: int = 60,
        on_recorded: Callable[[MaintenanceRun], Awaitable[None]] | None = None,
    ) -> None:
        self._jobs = list(jobs)
        self._bus = bus
        self._tenant = default_tenant
        self._timezone = timezone
        self._schedule = schedule
        self._poll_interval_s = poll_interval_s
        # Invoked with every completed run — success, per-job errors, or shutdown-interrupted
        # (#733's persisted history). Mirrors AutomationRunner.on_recorded: this class stays
        # unaware of what consumes it, and a write failure here must never cost the batch that
        # already did its real work — see the try/except at each call site below.
        self._on_recorded = on_recorded
        # In-memory only, like the rest of this scheduler's state (a restart re-evaluates due-ness
        # fresh against the wall clock — the same characteristic the old sleep_until_hour design
        # had). Set right before attempting a fire (not only on success), so a run that skips on
        # MaintenanceRunConflictError doesn't retry-storm every tick for the rest of the window —
        # it waits for the next one, matching the old design's one-attempt-per-window behavior.
        self._last_scheduled_fire: datetime | None = None
        self._last_run: MaintenanceRun | None = None
        self._current: MaintenanceCurrentRun | None = None
        # The tenant/source behind the in-flight run, if any — `shutdown` needs them to build
        # an interrupted `MaintenanceRun` for `_on_recorded`, since `MaintenanceCurrentRun`
        # itself carries neither (see `shutdown`'s docstring for why persistence lives there,
        # not in `_drive`'s own `except CancelledError`).
        self._current_tenant: str | None = None
        self._current_source: Literal["scheduled", "manual"] | None = None
        self._current_task: asyncio.Task[MaintenanceRun] | None = None

    def descriptors(self) -> list[dict[str, object]]:
        """The registered jobs as ``{key, label, nightly}`` dicts (for the UI)."""
        return [{"key": j.key, "label": j.label, "nightly": j.nightly} for j in self._jobs]

    def last_run(self) -> MaintenanceRun | None:
        """The most recent *completed* run's result, or ``None`` if none has finished yet."""
        return self._last_run

    def current_run(self) -> MaintenanceCurrentRun | None:
        """The in-flight batch's live per-job progress, or ``None`` if nothing is running."""
        return self._current

    def start_run(
        self,
        *,
        tenant: str | None = None,
        scope: Literal["all", "nightly"] = "all",
        source: Literal["scheduled", "manual"] = "manual",
    ) -> MaintenanceCurrentRun:
        """Start the batch as a detached background task and return its live progress immediately.

        Decoupled from any HTTP request: the driver task keeps running to completion regardless
        of whether the caller is still around to see it (#561) — the batch is not tied to a
        request's lifetime. Raises :class:`MaintenanceRunConflictError` (carrying the in-flight
        run) if a batch is already running, so a racing manual trigger or an overlapping nightly
        schedule joins it instead of starting a second one. ``source`` defaults to ``"manual"``
        — today's only other caller, :meth:`_tick`, passes ``"scheduled"`` explicitly; it exists
        for #733's persisted run history, which the ledger fields below feed.
        """
        if self._current is not None:
            raise MaintenanceRunConflictError(self._current)
        who = tenant or self._tenant
        selected = [j for j in self._jobs if scope == "all" or j.nightly]
        current = MaintenanceCurrentRun(
            started_at=datetime.now(UTC).isoformat(),
            scope=scope,
            jobs=[MaintenanceJobProgress(key=j.key, label=j.label) for j in selected],
        )
        self._current = current
        self._current_tenant = who
        self._current_source = source
        self._current_task = asyncio.create_task(self._drive(current, selected, who, source))
        return current

    async def run(
        self,
        *,
        tenant: str | None = None,
        scope: Literal["all", "nightly"] = "all",
        source: Literal["scheduled", "manual"] = "manual",
    ) -> MaintenanceRun:
        """Run the batch to completion and return its result (for the nightly scheduler + tests).

        Delegates to :meth:`start_run` — see there for the concurrent-run guard — and awaits the
        driver task, so it shares the exact same live progress and sequencing. The HTTP route
        uses :meth:`start_run` directly instead, since it must return without waiting (#561).
        """
        self.start_run(tenant=tenant, scope=scope, source=source)
        task = self._current_task
        assert task is not None  # start_run always sets it when it doesn't raise
        return await task

    async def _drive(
        self,
        current: MaintenanceCurrentRun,
        jobs: list[MaintenanceJob],
        tenant: str,
        source: Literal["scheduled", "manual"],
    ) -> MaintenanceRun:
        """Sequence *jobs*, updating *current*'s per-job status live; publish + cache on completion.

        A shutdown cancellation (:meth:`shutdown`) marks whatever hasn't finished as ``error`` and
        clears the current-run pointer **synchronously** — no ``await`` after catching
        ``CancelledError``, so cleanup can't be re-cancelled or race torn-down infra (the same
        rule ``agent/live_runs.py`` follows) — then re-raises; the batch is discarded (not
        published, not handed to ``_on_recorded``) the same as a cancelled chat turn. It is
        still persisted to history, but from :meth:`shutdown` after the cancellation has been
        absorbed — the one place downstream of this method that can safely ``await`` again.
        """
        results: list[MaintenanceJobResult] = []
        try:
            for progress, job in zip(current.jobs, jobs, strict=True):
                progress.status = "running"
                try:
                    status, detail = await job.run()
                except Exception as exc:  # one job's failure must never abort the batch
                    log.warning("maintenance job failed", job=job.key, error=str(exc))
                    status, detail = "error", str(exc)
                progress.status = status
                progress.detail = detail
                results.append(
                    MaintenanceJobResult(key=job.key, label=job.label, status=status, detail=detail)
                )
        except asyncio.CancelledError:
            for progress in current.jobs:
                if progress.status in ("pending", "running"):
                    progress.status = "error"
                    progress.detail = "interrupted by shutdown"
            self._current = None
            raise
        run = MaintenanceRun(
            ran_at=current.started_at,
            scope=current.scope,
            jobs=results,
            tenant=tenant,
            source=source,
            finished_at=datetime.now(UTC).isoformat(),
        )
        self._last_run = run
        # Persist BEFORE clearing `_current`, not after: the HTTP status route reads `last_run`
        # from the history store and `current_run` from `_current` as two independent calls with
        # no shared transaction, so a poller must never be able to observe `current_run: None`
        # (this run just finished) and `last_run: None` (the write hasn't landed yet) at once —
        # exactly the window this ordering closes.
        if self._on_recorded is not None:
            try:
                await self._on_recorded(run)
            except Exception as exc:  # a history-store hiccup must not fail the run itself
                log.warning("maintenance run history not recorded", error=str(exc))
        self._current = None
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
                tenant_id=tenant,
            )
        except Exception as exc:  # a NATS hiccup must not fail the run the operator just triggered
            log.warning("maintenance event publish failed", error=str(exc))
        log.info(
            "maintenance batch complete",
            scope=current.scope,
            jobs=len(results),
            errors=sum(1 for r in results if r.status == "error"),
        )
        return run

    async def shutdown(self) -> None:
        """Cancel and await the in-flight batch, if any — call once, during app shutdown.

        Marks the run interrupted **directly** rather than relying solely on ``_drive``'s own
        ``except CancelledError``: if the task hasn't taken its first step yet, cancelling it
        raises *before* its ``try`` runs, so that handler would never fire and ``current_run``
        would wedge non-``None`` forever (the same trap ``LiveRunRegistry.cancel`` in
        ``agent/live_runs.py`` guards against). Mirrors its cancel-then-await dance otherwise:
        never leave a task running against infra (the bus, the DB engine) that's about to close.

        An interrupted batch is discarded, not published (see :meth:`_drive`) — but it *is*
        handed to ``_on_recorded`` (#733), from here rather than from ``_drive``'s own
        ``except CancelledError``, which cannot safely ``await`` again once caught. Infra is
        still up at this point (shutdown order: this runs before the engine/bus close), so the
        write is safe; a failure is logged and swallowed, same posture as every other
        ``_on_recorded`` call.
        """
        task = self._current_task
        current = self._current
        if task is not None and not task.done():
            task.cancel()
        if current is not None:
            for progress in current.jobs:
                if progress.status in ("pending", "running"):
                    progress.status = "error"
                    progress.detail = "interrupted by shutdown"
        self._current = None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if current is not None and self._on_recorded is not None:
            interrupted = MaintenanceRun(
                ran_at=current.started_at,
                scope=current.scope,
                jobs=[
                    MaintenanceJobResult(
                        key=p.key, label=p.label, status=_terminal(p.status), detail=p.detail
                    )
                    for p in current.jobs
                ],
                tenant=self._current_tenant or self._tenant,
                source=self._current_source or "manual",
                finished_at=datetime.now(UTC).isoformat(),
            )
            try:
                await self._on_recorded(interrupted)
            except Exception as exc:
                log.warning("interrupted maintenance run not recorded", error=str(exc))

    async def run_periodic(self) -> None:
        """Poll every ``poll_interval_s`` and run the nightly batch when the schedule is due.

        A plain poll loop, not a single ``sleep_until_hour`` — the schedule is now dynamic and
        operator-editable at runtime (#621: enable/disable, cadence, hour, weekday), so a fixed
        sleep computed once at wake-time can't react to a change made while it's sleeping. Never
        exits (even while disabled): an operator can toggle it on later in the same process
        lifetime, and each tick re-reads the schedule fresh. Never dies on a transient error.
        """
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self._tick()
            except Exception as exc:  # a bad tick must not kill the scheduler
                log.warning("maintenance schedule tick failed", error=str(exc))

    async def _tick(self) -> None:
        """Evaluate the current schedule against local time; run the nightly batch if due."""
        schedule = await self._schedule()
        tz: tzinfo
        try:
            tz = ZoneInfo((await self._timezone()).strip() or "UTC")
        except Exception:  # unknown/blank/bad tz — fall back to UTC rather than skip the tick
            tz = UTC
        local_now = datetime.now(tz)
        if not is_due(schedule, local_now, self._last_scheduled_fire):
            return
        self._last_scheduled_fire = datetime.now(UTC)
        try:
            await self.run(scope="nightly", source="scheduled")
        except MaintenanceRunConflictError:
            log.info("nightly maintenance skipped; a manual run is already in progress")
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

    *reembed* is :meth:`ModuleRegistry.reembed`; it returns ``[{module, status, reason?}]``
    best-effort per module. A full re-embed is costly and only needed after the embedding model
    changes, so it is excluded from the scheduled batch and included only in the manual "run
    everything" trigger.

    A **refusal** is not a failure (#848): a module that answers ``refused`` because rebuilding
    would de-index a source that has gone wholesale empty did exactly what it should, and
    reporting the job as ``error`` for it would train the operator to ignore a working safety
    valve. Refusals are named in the detail with their reason and leave the status ``ok``; only
    a genuine error still fails the job.
    """

    async def _run() -> tuple[JobStatus, str]:
        results = await reembed()
        if not results:
            return "skipped", "no reindexable modules"
        started = sum(1 for r in results if r.get("status") == "started")
        refused = [
            f"{r['module']} ({r['reason']})" if r.get("reason") else r["module"]
            for r in results
            if r.get("status") == "refused"
        ]
        failed = [r["module"] for r in results if r.get("status") not in ("started", "refused")]
        detail = f"re-index started on {started}/{len(results)} module(s)"
        if refused:
            detail += f"; refused by the de-index fuse: {', '.join(refused)}"
        if failed:
            detail += f"; failed: {', '.join(failed)}"
        status: JobStatus = "error" if failed and not started else "ok"
        return status, detail

    return MaintenanceJob(
        key="module-reindex", label="Module re-index / re-embed", run=_run, nightly=False
    )


def profile_synthesis_job(synthesize: Callable[[], Awaitable[int]]) -> MaintenanceJob:
    """Synthesize each tenant's standing profile from its facts (ADR-0094) — light, nightly-run.

    *synthesize* is :meth:`ProfileSynthesizer.run`; it is best-effort per tenant, skips a paused
    gateway, and preserves an operator-pinned profile, so this job only reports how many profiles it
    (re)wrote. Distilling a few hundred tokens per tenant is cheap — the whole point is to pay it
    off-hours instead of on every turn — so it rides the nightly batch, not the manual-only tier.
    """

    async def _run() -> tuple[JobStatus, str]:
        count = await synthesize()
        return "ok", f"synthesized {count} standing profile(s)"

    return MaintenanceJob(
        key="memory-profile", label="Memory standing-profile synthesis", run=_run, nightly=True
    )


def playbook_reflection_job(
    reflect: Callable[[], Awaitable[int]], *, enabled: bool = True
) -> MaintenanceJob:
    """Propose edits to the agent's playbooks from recent sessions (ADR-0093 §1, #762) — nightly.

    *reflect* is :meth:`PlaybookReflector.run`; it is best-effort per tenant, skips a paused
    gateway, and only ever *stages* playbook proposals for the operator's approval — it never
    writes the agent's instructions or playbooks — so this job reports how many now await
    review. One gateway call per active tenant over a night's conversations is light enough for
    the scheduled batch, the same tier as the extraction drain and profile synthesis.

    ``enabled`` is the feature's own off-switch (#762, ``PLAYBOOK_REFLECTION_ENABLED``): the job
    stays registered — the maintenance panel keeps showing it, honestly, as skipped — but a
    disabled pass spends **no gateway call** and says so in its status line. Before this knob
    existed, the only way to stop reflection was disabling all nightly maintenance.
    """

    async def _run() -> tuple[JobStatus, str]:
        if not enabled:
            return "skipped", "disabled (PLAYBOOK_REFLECTION_ENABLED=false)"
        count = await reflect()
        return "ok", f"staged {count} proposal(s) for review"

    return MaintenanceJob(
        key="playbook-reflection", label="Agent playbook reflection", run=_run, nightly=True
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


def prune_run_history_job(prune: Callable[[], Awaitable[int]]) -> MaintenanceJob:
    """Trim the persisted maintenance-run history (#733) — light, so nightly-eligible.

    *prune* is a zero-arg closure over :meth:`MaintenanceRunStore.prune` for the default
    tenant; it drops rows past the retention cap (row count and/or age). Runs as an ordinary
    job in the batch it is itself trimming history *for* — the row this very run produces is
    written only after the whole batch (this job included) completes, so a job never prunes
    its own about-to-be-inserted row.
    """

    async def _run() -> tuple[JobStatus, str]:
        pruned = await prune()
        return "ok", f"pruned {pruned} old run(s)"

    return MaintenanceJob(
        key="run-history-prune", label="Maintenance run history prune", run=_run, nightly=True
    )
