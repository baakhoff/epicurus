"""Tests for the maintenance API (ADR-0060), ``/platform/v1/maintenance`` — over the ASGI app.

The crux of the #561 coverage is :func:`test_post_run_does_not_block_on_a_gated_job`: ``POST
/run`` must return before a slow batch finishes, not hold the request open for it (a full
re-embed can take minutes). If that regresses, this test hangs until pytest's timeout kills it —
proof by construction rather than a timing assertion.
"""

from __future__ import annotations

import asyncio
import contextlib
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


def _gated_job(key: str, gate: asyncio.Event, *, nightly: bool = True) -> MaintenanceJob:
    """A job that stays ``running`` until *gate* is set — for observing in-flight state."""

    async def run() -> tuple[JobStatus, str]:
        await gate.wait()
        return "ok", f"{key} done"

    return MaintenanceJob(key=key, label=key.title(), run=run, nightly=nightly)


@contextlib.asynccontextmanager
async def _client_for(jobs: list[MaintenanceJob]) -> AsyncIterator[AsyncClient]:
    orch = MaintenanceOrchestrator(
        jobs,
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


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with _client_for([_job("memory-extraction"), _job("module-reindex", nightly=False)]) as c:
        yield c


async def _poll_until_idle(client: AsyncClient, *, attempts: int = 300) -> dict[str, Any]:
    """Poll ``GET`` until ``current_run`` clears — the HTTP-level way to await completion."""
    for _ in range(attempts):
        body: dict[str, Any] = (await client.get("/platform/v1/maintenance")).json()
        if body["current_run"] is None:
            return body
        await asyncio.sleep(0.01)
    pytest.fail("maintenance run never completed")


async def test_status_lists_jobs_and_schedule(client: AsyncClient) -> None:
    resp = await client.get("/platform/v1/maintenance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schedule_enabled"] is False and body["schedule_hour"] == 4
    assert [j["key"] for j in body["jobs"]] == ["memory-extraction", "module-reindex"]
    assert body["jobs"][1]["nightly"] is False
    assert body["last_run"] is None
    assert body["current_run"] is None


async def test_run_returns_202_with_pending_progress_then_completion_updates_last_run(
    client: AsyncClient,
) -> None:
    resp = await client.post("/platform/v1/maintenance/run")
    assert resp.status_code == 202
    body = resp.json()
    assert body["scope"] == "all"
    assert {j["key"] for j in body["jobs"]} == {"memory-extraction", "module-reindex"}
    assert body["started_at"]  # ISO timestamp present
    # every job status is a valid live-progress value (jobs may already have finished by the
    # time this response is decoded — the batch runs concurrently with the response send).
    assert all(j["status"] in ("pending", "running", "ok") for j in body["jobs"])

    status = await _poll_until_idle(client)
    assert status["last_run"]["scope"] == "all"
    assert len(status["last_run"]["jobs"]) == 2
    assert all(j["status"] == "ok" for j in status["last_run"]["jobs"])


async def test_post_run_does_not_block_on_a_gated_job() -> None:
    """The core #561 fix: the request returns without waiting for the batch to finish.

    The gate is never set — if ``POST /run`` still awaited the batch inline (the pre-#561
    bug), this would hang until pytest's global timeout kills it rather than returning 202.
    """
    gate = asyncio.Event()
    async with _client_for([_gated_job("slow", gate)]) as client:
        resp = await asyncio.wait_for(client.post("/platform/v1/maintenance/run"), timeout=2)
        assert resp.status_code == 202
        assert resp.json()["jobs"][0]["status"] in ("pending", "running")

        status = (await client.get("/platform/v1/maintenance")).json()
        assert status["current_run"] is not None
        assert status["last_run"] is None  # still in flight — nothing published yet

        gate.set()
        await _poll_until_idle(client)


async def test_concurrent_post_run_returns_409_and_joins_the_inflight_run() -> None:
    gate = asyncio.Event()
    async with _client_for([_gated_job("slow", gate)]) as client:
        first = await client.post("/platform/v1/maintenance/run")
        assert first.status_code == 202
        started_at = first.json()["started_at"]

        second = await client.post("/platform/v1/maintenance/run")
        assert second.status_code == 409

        # No second batch was started — GET still shows the exact same in-flight run.
        status = (await client.get("/platform/v1/maintenance")).json()
        assert status["current_run"]["started_at"] == started_at

        gate.set()
        await _poll_until_idle(client)


async def test_get_exposes_current_run_shape_while_running() -> None:
    gate = asyncio.Event()
    async with _client_for([_gated_job("slow", gate, nightly=False)]) as client:
        await client.post("/platform/v1/maintenance/run")
        status = (await client.get("/platform/v1/maintenance")).json()
        current = status["current_run"]
        assert current["scope"] == "all"
        assert current["started_at"]
        assert current["jobs"] == [
            {
                "key": "slow",
                "label": "Slow",
                "status": current["jobs"][0]["status"],  # "pending" or "running" — timing-dependent
                "detail": "",
            }
        ]
        assert current["jobs"][0]["status"] in ("pending", "running")

        gate.set()
        await _poll_until_idle(client)
