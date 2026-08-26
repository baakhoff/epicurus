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
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core_app.maintenance import JobStatus, MaintenanceJob, MaintenanceOrchestrator
from epicurus_core_app.maintenance_history import MaintenanceRunStore
from epicurus_core_app.maintenance_routes import create_maintenance_router
from epicurus_core_app.maintenance_schedule_prefs import MaintenanceScheduleStore

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
async def _client_for(
    tmp_path: Path,
    jobs: list[MaintenanceJob],
    *,
    default_enabled: bool = False,
    default_hour: int = 4,
) -> AsyncIterator[AsyncClient]:
    # File-backed, not `:memory:` on StaticPool: `POST /run` fires the batch as a detached
    # background task (#561) that writes history concurrently with this fixture's own request
    # handlers reading the schedule store — two tasks against one shared StaticPool connection
    # races a reader's checkout-return ROLLBACK against the writer's BEGIN…COMMIT and can
    # silently drop the write (the in-memory-SQLite/StaticPool trap; mirrors
    # test_automations_feed.py's `_engine`). A file is one shared DB across as many
    # connections/tasks as touch it.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'maintenance.db'}")
    try:
        schedule_store = MaintenanceScheduleStore(
            engine, default_enabled=default_enabled, default_hour=default_hour
        )
        await schedule_store.init()
        history = MaintenanceRunStore(engine)
        await history.init()
        orch = MaintenanceOrchestrator(
            jobs,
            bus=_FakeBus(),  # type: ignore[arg-type]
            default_tenant=TENANT,
            timezone=_tz,
            schedule=lambda: schedule_store.get(TENANT),
            on_recorded=history.record,
        )
        app = FastAPI()
        app.include_router(
            create_maintenance_router(
                orch,
                schedule_store=schedule_store,
                history=history,
                timezone=_tz,
                default_tenant=TENANT,
            )
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as c:
            yield c
    finally:
        await engine.dispose()


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    async with _client_for(
        tmp_path, [_job("memory-extraction"), _job("module-reindex", nightly=False)]
    ) as c:
        yield c


async def _poll_until_idle(
    client: AsyncClient, *, after_id: int | None, attempts: int = 300
) -> dict[str, Any]:
    """Poll ``GET`` until a run newer than *after_id* has completed and landed in ``last_run``.

    ``current_run`` reads ``None`` both *before* a run starts and *after* it finishes —
    polling for "cleared" alone races the background task's very first checkpoint
    (``POST /run`` returns before the batch runs, #561) and can report the pre-run idle
    state instead of waiting at all (#833). It's worse on repeated runs in the same
    test: a *stale* ``last_run`` from a prior call is just as non-``None`` as a fresh
    one, so "current_run is None and last_run is not None" isn't enough either.
    *after_id* — ``last_run["id"]`` (or ``None`` for a client that has never run) as it
    stood *before* the triggering ``POST`` — disambiguates the two: history ids are
    assigned on persist and only ever increase, so the first ``last_run`` whose id
    differs from *after_id* is unambiguously the new one. (A ``started_at`` string
    comparison looks tempting instead, but the value round-trips through SQLite's
    ``DateTime`` column and loses its UTC offset on the way — ``+00:00`` in, bare
    naive-looking string out — so it never compares equal to the one the ``POST``
    response carried.) Callers chain calls by feeding each return's ``last_run["id"]``
    back in as the next ``after_id``.
    """
    for _ in range(attempts):
        body: dict[str, Any] = (await client.get("/platform/v1/maintenance")).json()
        last = body["last_run"]
        if body["current_run"] is None and last is not None and last["id"] != after_id:
            return body
        await asyncio.sleep(0.01)
    pytest.fail("maintenance run never completed")


async def test_status_lists_jobs_and_schedule(client: AsyncClient) -> None:
    resp = await client.get("/platform/v1/maintenance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schedule_enabled"] is False
    assert body["schedule_cadence"] == "daily" and body["schedule_hour"] == 4
    assert body["schedule_weekday"] is None
    assert body["next_run_at"] is None  # disabled — no next run to report
    assert [j["key"] for j in body["jobs"]] == ["memory-extraction", "module-reindex"]
    assert body["jobs"][1]["nightly"] is False
    assert body["last_run"] is None
    assert body["current_run"] is None


# ── schedule GET/PUT (#621) ──────────────────────────────────────────────────────


async def test_put_schedule_persists_and_get_reflects_it(client: AsyncClient) -> None:
    resp = await client.put(
        "/platform/v1/maintenance/schedule",
        json={"enabled": True, "cadence": "weekly", "hour": 3, "weekday": 5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["schedule_enabled"] is True
    assert body["schedule_cadence"] == "weekly"
    assert body["schedule_hour"] == 3
    assert body["schedule_weekday"] == 5
    assert body["next_run_at"] is not None  # enabled — a next run is estimated

    status = (await client.get("/platform/v1/maintenance")).json()
    assert status["schedule_enabled"] is True
    assert status["schedule_cadence"] == "weekly"
    assert status["schedule_hour"] == 3
    assert status["schedule_weekday"] == 5


async def test_put_schedule_rejects_an_unknown_cadence(client: AsyncClient) -> None:
    resp = await client.put(
        "/platform/v1/maintenance/schedule",
        json={"enabled": True, "cadence": "monthly", "hour": 4},
    )
    assert resp.status_code == 400
    # A rejected PUT must not have persisted — the default is untouched.
    status = (await client.get("/platform/v1/maintenance")).json()
    assert status["schedule_enabled"] is False


async def test_put_schedule_rejects_weekly_without_a_weekday(client: AsyncClient) -> None:
    resp = await client.put(
        "/platform/v1/maintenance/schedule",
        json={"enabled": True, "cadence": "weekly", "hour": 4},
    )
    assert resp.status_code == 400


async def test_put_schedule_rejects_an_out_of_range_hour(client: AsyncClient) -> None:
    resp = await client.put(
        "/platform/v1/maintenance/schedule",
        json={"enabled": True, "cadence": "daily", "hour": 24},
    )
    assert resp.status_code == 400


async def test_get_reflects_the_env_configured_default_when_never_set(tmp_path: Path) -> None:
    async with _client_for(
        tmp_path, [_job("a")], default_enabled=True, default_hour=7
    ) as client_with_default:
        status = (await client_with_default.get("/platform/v1/maintenance")).json()
        assert status["schedule_enabled"] is True
        assert status["schedule_cadence"] == "daily" and status["schedule_hour"] == 7
        assert status["next_run_at"] is not None


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

    status = await _poll_until_idle(client, after_id=None)
    assert status["last_run"]["scope"] == "all"
    assert len(status["last_run"]["jobs"]) == 2
    assert all(j["status"] == "ok" for j in status["last_run"]["jobs"])


async def test_post_run_does_not_block_on_a_gated_job(tmp_path: Path) -> None:
    """The core #561 fix: the request returns without waiting for the batch to finish.

    The gate is never set — if ``POST /run`` still awaited the batch inline (the pre-#561
    bug), this would hang until pytest's global timeout kills it rather than returning 202.
    """
    gate = asyncio.Event()
    async with _client_for(tmp_path, [_gated_job("slow", gate)]) as client:
        resp = await asyncio.wait_for(client.post("/platform/v1/maintenance/run"), timeout=2)
        assert resp.status_code == 202
        run = resp.json()
        assert run["jobs"][0]["status"] in ("pending", "running")

        status = (await client.get("/platform/v1/maintenance")).json()
        assert status["current_run"] is not None
        assert status["last_run"] is None  # still in flight — nothing published yet

        gate.set()
        await _poll_until_idle(client, after_id=None)


async def test_concurrent_post_run_returns_409_and_joins_the_inflight_run(tmp_path: Path) -> None:
    gate = asyncio.Event()
    async with _client_for(tmp_path, [_gated_job("slow", gate)]) as client:
        first = await client.post("/platform/v1/maintenance/run")
        assert first.status_code == 202
        started_at = first.json()["started_at"]

        second = await client.post("/platform/v1/maintenance/run")
        assert second.status_code == 409

        # No second batch was started — GET still shows the exact same in-flight run.
        status = (await client.get("/platform/v1/maintenance")).json()
        assert status["current_run"]["started_at"] == started_at

        gate.set()
        await _poll_until_idle(client, after_id=None)


async def test_get_exposes_current_run_shape_while_running(tmp_path: Path) -> None:
    gate = asyncio.Event()
    async with _client_for(tmp_path, [_gated_job("slow", gate, nightly=False)]) as client:
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
        await _poll_until_idle(client, after_id=None)


# ── persisted run history (#733) ──────────────────────────────────────────────────


async def test_manual_run_history_reports_source_manual(client: AsyncClient) -> None:
    await client.post("/platform/v1/maintenance/run")
    status = await _poll_until_idle(client, after_id=None)
    last = status["last_run"]
    assert last["source"] == "manual"
    assert last["id"] is not None
    assert last["started_at"] and last["finished_at"]


async def test_run_history_page_lists_newest_first(client: AsyncClient) -> None:
    await client.post("/platform/v1/maintenance/run")
    first_status = await _poll_until_idle(client, after_id=None)
    await client.post("/platform/v1/maintenance/run")
    await _poll_until_idle(client, after_id=first_status["last_run"]["id"])

    page = (await client.get("/platform/v1/maintenance/runs")).json()
    assert len(page["runs"]) == 2
    assert page["runs"][0]["id"] > page["runs"][1]["id"]  # newest first
    assert page["next_cursor"] is None  # both runs fit on one page


async def test_run_history_page_respects_limit_and_cursor(client: AsyncClient) -> None:
    after_id = None
    for _ in range(3):
        await client.post("/platform/v1/maintenance/run")
        status = await _poll_until_idle(client, after_id=after_id)
        after_id = status["last_run"]["id"]

    first_page = (await client.get("/platform/v1/maintenance/runs", params={"limit": 2})).json()
    assert len(first_page["runs"]) == 2
    assert first_page["next_cursor"] is not None

    second_page = (
        await client.get(
            "/platform/v1/maintenance/runs",
            params={"limit": 2, "cursor": first_page["next_cursor"]},
        )
    ).json()
    assert len(second_page["runs"]) == 1
    assert second_page["next_cursor"] is None
    # no overlap between pages
    first_ids = {r["id"] for r in first_page["runs"]}
    assert second_page["runs"][0]["id"] not in first_ids


async def test_run_history_is_empty_before_any_run(client: AsyncClient) -> None:
    page = (await client.get("/platform/v1/maintenance/runs")).json()
    assert page == {"runs": [], "next_cursor": None}
