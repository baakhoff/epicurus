"""Tests for the maintenance API (ADR-0060), ``/platform/v1/maintenance`` — over the ASGI app."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from epicurus_core_app.maintenance import JobStatus, MaintenanceJob, MaintenanceOrchestrator
from epicurus_core_app.maintenance_routes import create_maintenance_router

TENANT = "local"


class _FakeBus:
    async def publish(
        self, subject: str, data: dict[str, Any], tenant_id: str | None = None
    ) -> None:
        return None


async def _tz() -> str:
    return "UTC"


def _job(key: str, *, nightly: bool = True) -> MaintenanceJob:
    async def run() -> tuple[JobStatus, str]:
        return "ok", f"{key} done"

    return MaintenanceJob(key=key, label=key.title(), run=run, nightly=nightly)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    orch = MaintenanceOrchestrator(
        [_job("memory-extraction"), _job("module-reindex", nightly=False)],
        bus=_FakeBus(),  # type: ignore[arg-type]
        default_tenant=TENANT,
        timezone=_tz,
        hour=4,
        schedule_enabled=False,
    )
    app = FastAPI()
    app.include_router(create_maintenance_router(orch, default_tenant=TENANT))
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as c:
        yield c


async def test_status_lists_jobs_and_schedule(client: AsyncClient) -> None:
    resp = await client.get("/platform/v1/maintenance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schedule_enabled"] is False and body["schedule_hour"] == 4
    assert [j["key"] for j in body["jobs"]] == ["memory-extraction", "module-reindex"]
    assert body["jobs"][1]["nightly"] is False
    assert body["last_run"] is None


async def test_run_executes_all_jobs_and_records_last_run(client: AsyncClient) -> None:
    resp = await client.post("/platform/v1/maintenance/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "all"
    assert {j["key"] for j in body["jobs"]} == {"memory-extraction", "module-reindex"}
    assert all(j["status"] == "ok" for j in body["jobs"])
    assert body["ran_at"]  # ISO timestamp present

    # The run is now cached and surfaced by the status endpoint.
    status = (await client.get("/platform/v1/maintenance")).json()
    assert status["last_run"]["scope"] == "all"
    assert len(status["last_run"]["jobs"]) == 2
