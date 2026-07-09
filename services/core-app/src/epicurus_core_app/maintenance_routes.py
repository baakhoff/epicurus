"""The maintenance-orchestrator platform API (ADR-0060), under ``/platform/v1/maintenance``.

``GET`` reports the registered jobs, the schedule, the last completed run, and any run currently
in flight. ``POST /run`` starts the manual "run everything" batch as a background task and
returns immediately (202) with that run's live progress — it does not wait for the batch, which
can take minutes (#561). A second ``POST`` while one is already running responds 409 rather than
starting a competing batch; the caller re-``GET``s to observe/join the in-flight run. The shell's
Settings screen drives it: it rehydrates onto ``current_run`` on mount and polls while one is live.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from epicurus_core_app.maintenance import (
    MaintenanceCurrentRun,
    MaintenanceOrchestrator,
    MaintenanceRun,
    MaintenanceRunConflictError,
)


class MaintenanceJobView(BaseModel):
    """A registered job, as advertised to the UI."""

    key: str
    label: str
    nightly: bool


class MaintenanceJobResultView(BaseModel):
    """One job's outcome in a completed run."""

    key: str
    label: str
    status: str
    detail: str


class MaintenanceRunView(BaseModel):
    """The aggregate result of one completed batch."""

    ran_at: str
    scope: str
    jobs: list[MaintenanceJobResultView]


class MaintenanceJobProgressView(BaseModel):
    """One job's live status within an in-flight run — ``pending``/``running`` included."""

    key: str
    label: str
    status: str
    detail: str


class MaintenanceCurrentRunView(BaseModel):
    """An in-flight batch, as advertised to the UI — polled while live (#561)."""

    started_at: str
    scope: str
    jobs: list[MaintenanceJobProgressView]


class MaintenanceStatusView(BaseModel):
    """The maintenance surface: schedule, registered jobs, the last run, and any live run."""

    schedule_enabled: bool
    schedule_hour: int
    jobs: list[MaintenanceJobView]
    last_run: MaintenanceRunView | None
    current_run: MaintenanceCurrentRunView | None


def _run_view(run: MaintenanceRun) -> MaintenanceRunView:
    return MaintenanceRunView(
        ran_at=run.ran_at,
        scope=run.scope,
        jobs=[
            MaintenanceJobResultView(key=r.key, label=r.label, status=r.status, detail=r.detail)
            for r in run.jobs
        ],
    )


def _current_view(current: MaintenanceCurrentRun) -> MaintenanceCurrentRunView:
    return MaintenanceCurrentRunView(
        started_at=current.started_at,
        scope=current.scope,
        jobs=[
            MaintenanceJobProgressView(key=p.key, label=p.label, status=p.status, detail=p.detail)
            for p in current.jobs
        ],
    )


def create_maintenance_router(
    orchestrator: MaintenanceOrchestrator, *, default_tenant: str = "local"
) -> APIRouter:
    """Build the ``/platform/v1/maintenance`` router over a :class:`MaintenanceOrchestrator`."""
    router = APIRouter(prefix="/platform/v1/maintenance", tags=["maintenance"])

    @router.get("", response_model=MaintenanceStatusView)
    async def maintenance_status() -> MaintenanceStatusView:
        last = orchestrator.last_run()
        current = orchestrator.current_run()
        return MaintenanceStatusView(
            schedule_enabled=orchestrator.schedule_enabled,
            schedule_hour=orchestrator.schedule_hour,
            jobs=[
                MaintenanceJobView(
                    key=str(d["key"]), label=str(d["label"]), nightly=bool(d["nightly"])
                )
                for d in orchestrator.descriptors()
            ],
            last_run=_run_view(last) if last else None,
            current_run=_current_view(current) if current else None,
        )

    @router.post(
        "/run", response_model=MaintenanceCurrentRunView, status_code=status.HTTP_202_ACCEPTED
    )
    async def run_maintenance() -> MaintenanceCurrentRunView:
        try:
            current = orchestrator.start_run(tenant=default_tenant, scope="all")
        except MaintenanceRunConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return _current_view(current)

    return router
