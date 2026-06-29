"""The maintenance-orchestrator platform API (ADR-0060), under ``/platform/v1/maintenance``.

``GET`` reports the registered jobs, the schedule, and the last run; ``POST /run`` triggers the
manual "run everything" batch and returns its per-job result. The shell's Settings screen drives it.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from epicurus_core_app.maintenance import MaintenanceOrchestrator, MaintenanceRun


class MaintenanceJobView(BaseModel):
    """A registered job, as advertised to the UI."""

    key: str
    label: str
    nightly: bool


class MaintenanceJobResultView(BaseModel):
    """One job's outcome in a run."""

    key: str
    label: str
    status: str
    detail: str


class MaintenanceRunView(BaseModel):
    """The aggregate result of one batch."""

    ran_at: str
    scope: str
    jobs: list[MaintenanceJobResultView]


class MaintenanceStatusView(BaseModel):
    """The maintenance surface: schedule, registered jobs, and the last run (if any)."""

    schedule_enabled: bool
    schedule_hour: int
    jobs: list[MaintenanceJobView]
    last_run: MaintenanceRunView | None


def _run_view(run: MaintenanceRun) -> MaintenanceRunView:
    return MaintenanceRunView(
        ran_at=run.ran_at,
        scope=run.scope,
        jobs=[
            MaintenanceJobResultView(key=r.key, label=r.label, status=r.status, detail=r.detail)
            for r in run.jobs
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
        )

    @router.post("/run", response_model=MaintenanceRunView)
    async def run_maintenance() -> MaintenanceRunView:
        return _run_view(await orchestrator.run(tenant=default_tenant, scope="all"))

    return router
