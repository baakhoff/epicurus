"""The operator-facing portability API — ``/platform/v1/portability`` (#867).

Seven endpoints, three shapes. An **export** is started, polled, and downloaded; an
**import** is uploaded (which only ever *reads* it), previewed, applied, and polled. The
asymmetry is deliberate: an export can be started with one click because it changes nothing,
while an import shows the operator exactly what it is about to do and waits to be told to do
it. The seventh, **the job list**, is what makes either survive a page reload (#877).

Every endpoint is tenant-scoped, and a job id from another tenant reads as absent rather
than forbidden (constraint #1 — a tenant should not be able to learn that another's job
exists).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from epicurus_core import get_logger
from epicurus_core.tenancy import TenantError, validate_tenant_id
from epicurus_core_app.portability.jobs import PortabilityJob
from epicurus_core_app.portability.models import (
    ArchiveManifest,
    ComponentEntry,
    ExportJobView,
    ImportJobView,
    ImportPreview,
    ImportReportView,
    PortabilityJobSummary,
)
from epicurus_core_app.portability.service import ArchiveTooLarge, PortabilityService

__all__ = ["create_portability_router"]

log = get_logger("core.portability.routes")

_UPLOAD_CHUNK = 1024 * 1024

# How far back the job list reaches. The list is a re-attachment aid, not a history: the
# operator needs the job they just started and the handful before it, and the sweep removes
# everything past the retention window anyway.
_JOB_LIST_LIMIT = 20


def create_portability_router(
    service: PortabilityService,
    *,
    default_tenant: str = "local",
    max_archive_bytes: int = 4 * 1024 * 1024 * 1024,
) -> APIRouter:
    """Build the ``/platform/v1/portability`` router."""
    router = APIRouter(prefix="/platform/v1/portability", tags=["portability"])

    def _tenant(tenant_id: str | None) -> str:
        try:
            return validate_tenant_id(tenant_id or default_tenant)
        except TenantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def _job(tenant: str, job_id: str, kind: str) -> PortabilityJob:
        job = await service.job(tenant=tenant, job_id=job_id)
        if job is None or job.kind != kind:
            raise HTTPException(status_code=404, detail=f"no {kind} job {job_id!r}")
        return job

    @router.get("/jobs", response_model=list[PortabilityJobSummary])
    async def list_jobs(
        tenant_id: str | None = Query(default=None),
    ) -> list[PortabilityJobSummary]:
        """This tenant's recent jobs, newest first, both kinds — how a reload re-attaches.

        Without it a job is reachable only by an id the browser tab holds in memory, so a
        refresh mid-export orphans the run: it finishes staging, is never offered, and is
        swept `PORTABILITY_RETENTION_HOURS` later.
        """
        tenant = _tenant(tenant_id)
        jobs = await service.recent(tenant=tenant, limit=_JOB_LIST_LIMIT)
        # One hop off the loop for the whole page rather than one ``stat`` per row: the list
        # is polled while a job runs, and twenty blocking syscalls a second is not free.
        staged = await asyncio.to_thread(_staged_archives, jobs)
        return [
            PortabilityJobSummary(
                id=job.id,
                kind=job.kind,
                status=job.status,
                created_at=job.created_at.isoformat(),
                updated_at=job.updated_at.isoformat(),
                archive_available=job.id in staged,
                size_bytes=job.size_bytes,
            )
            for job in jobs
        ]

    @router.post("/exports", response_model=ExportJobView, status_code=202)
    async def start_export(tenant_id: str | None = Query(default=None)) -> ExportJobView:
        """Start a tenant export. Returns at once; poll the job for progress."""
        tenant = _tenant(tenant_id)
        job = await service.start_export(tenant=tenant)
        return _export_view(await _job(tenant, job.id, "export"))

    @router.get("/exports/{job_id}", response_model=ExportJobView)
    async def get_export(job_id: str, tenant_id: str | None = Query(default=None)) -> ExportJobView:
        """Progress, and — once ready — the archive's manifest and size."""
        return _export_view(await _job(_tenant(tenant_id), job_id, "export"))

    @router.get("/exports/{job_id}/archive")
    async def download_export(
        job_id: str, tenant_id: str | None = Query(default=None)
    ) -> FileResponse:
        """The finished ``.tar.gz``. 409 while the job is still running or if it failed."""
        tenant = _tenant(tenant_id)
        job = await _job(tenant, job_id, "export")
        if job.status != "ready" or not job.archive_path:
            raise HTTPException(status_code=409, detail=f"export is {job.status}, not ready")
        path = Path(job.archive_path)
        if not path.exists():
            # Staging is a disposable cache (constraint #2): a swept archive is an expected
            # end state, not a server error — say so plainly so the UI offers a re-export.
            raise HTTPException(
                status_code=410, detail="the staged archive has expired; run the export again"
            )
        stamp = job.created_at.strftime("%Y%m%d-%H%M%S")
        return FileResponse(
            path,
            media_type="application/gzip",
            filename=f"epicurus-{tenant}-{stamp}.tar.gz",
        )

    @router.post("/imports", response_model=ImportJobView)
    async def stage_import(
        file: UploadFile, tenant_id: str | None = Query(default=None)
    ) -> ImportJobView:
        """Upload an archive and read it. **Applies nothing** — returns the preview."""
        tenant = _tenant(tenant_id)

        async def chunks() -> AsyncIterator[bytes]:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                yield chunk

        try:
            job = await service.stage_import(
                tenant=tenant, chunks=chunks(), max_bytes=max_archive_bytes
            )
        except ArchiveTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except Exception as exc:  # not a readable archive / no manifest
            raise HTTPException(status_code=400, detail=f"unreadable archive: {exc}") from exc
        return _import_view(job)

    @router.post("/imports/{job_id}/apply", response_model=ImportJobView, status_code=202)
    async def apply_import(
        job_id: str, tenant_id: str | None = Query(default=None)
    ) -> ImportJobView:
        """Apply a staged import in the background. Poll the job for the final report."""
        tenant = _tenant(tenant_id)
        job = await _job(tenant, job_id, "import")
        if job.status != "staged":
            raise HTTPException(status_code=409, detail=f"import is {job.status}, not staged")
        preview = ImportPreview.model_validate(job.preview) if job.preview else None
        if preview is None or not preview.compatible:
            raise HTTPException(
                status_code=409,
                detail=(preview.refusal if preview else "import has no preview")
                or "archive is not compatible with this core",
            )
        if not job.archive_path or not Path(job.archive_path).exists():
            raise HTTPException(
                status_code=410, detail="the staged archive has expired; upload it again"
            )
        applied = await service.start_apply(tenant=tenant, job_id=job_id)
        if applied is None:
            raise HTTPException(status_code=404, detail=f"no import job {job_id!r}")
        return _import_view(applied)

    @router.get("/imports/{job_id}", response_model=ImportJobView)
    async def get_import(job_id: str, tenant_id: str | None = Query(default=None)) -> ImportJobView:
        """Status, the preview, and — once applied — the final report."""
        return _import_view(await _job(_tenant(tenant_id), job_id, "import"))

    return router


def _staged_archives(jobs: list[PortabilityJob]) -> set[str]:
    """Which of *jobs* are downloadable right now — a ready export whose file is still there.

    Blocking (``Path.exists``), so it is called through a thread; the caller passes the whole
    page at once.
    """
    return {
        job.id
        for job in jobs
        if job.kind == "export"
        and job.status == "ready"
        and job.archive_path
        and Path(job.archive_path).exists()
    }


def _export_view(job: PortabilityJob) -> ExportJobView:
    status = job.status if job.status in ("running", "ready", "failed") else "running"
    return ExportJobView(
        id=job.id,
        status=status,  # type: ignore[arg-type]
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        progress=[ComponentEntry.model_validate(entry) for entry in job.progress],
        manifest=ArchiveManifest.model_validate(job.manifest) if job.manifest else None,
        size_bytes=job.size_bytes,
        error=job.error,
    )


def _import_view(job: PortabilityJob) -> ImportJobView:
    status = job.status if job.status in ("staged", "running", "done", "failed") else "staged"
    return ImportJobView(
        id=job.id,
        status=status,  # type: ignore[arg-type]
        created_at=job.created_at.isoformat(),
        updated_at=job.updated_at.isoformat(),
        preview=ImportPreview.model_validate(job.preview) if job.preview else None,
        report=ImportReportView.model_validate(job.report) if job.report else None,
        error=job.error,
    )
