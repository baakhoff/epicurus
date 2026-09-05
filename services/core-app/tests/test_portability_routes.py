"""The portability API (#867), ``/platform/v1/portability`` — over the real ASGI app.

What the Settings card actually talks to: start, poll, download; upload, preview, apply,
poll. The service underneath is the real one (no module fleet wired — the core's own data
and the file space are enough to exercise every route), so the responses here are the
responses the shell will see.
"""

from __future__ import annotations

import asyncio
import tarfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core_app.portability.core_data import CORE_SETS
from epicurus_core_app.portability.jobs import PortabilityJobStore
from epicurus_core_app.portability.routes import create_portability_router
from epicurus_core_app.portability.service import PortabilityService

TENANT = "local"


async def _engine(tmp_path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'core.db'}")
    async with engine.begin() as conn:
        for specs in CORE_SETS.values():
            for spec in specs:
                await conn.run_sync(spec.table.create, checkfirst=True)
    return engine


class _NoModules:
    async def snapshot(self, *, force: bool = False) -> list[Any]:
        return []

    async def base_url(self, name: str) -> str:
        raise RuntimeError(name)


async def _service(tmp_path: Path, engine: AsyncEngine, **kwargs: Any) -> PortabilityService:
    from epicurus_core.files import LocalFileStore

    jobs = PortabilityJobStore(engine)
    await jobs.init()
    store = LocalFileStore(tmp_path / "files")
    await store.ensure_tenant_root(tenant=TENANT)
    await store.write_bytes(tenant=TENANT, path="notes/a.md", data=b"note")
    return PortabilityService(
        jobs=jobs,
        engine=engine,
        file_store=store,
        registry=_NoModules(),
        staging_dir=tmp_path / "staging",
        core_app_version="0.118.0",
        **kwargs,
    )


def _client(service: PortabilityService, *, max_archive_bytes: int = 0) -> AsyncClient:
    app = FastAPI()
    app.include_router(
        create_portability_router(
            service, default_tenant=TENANT, max_archive_bytes=max_archive_bytes
        )
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://core")


async def _ready(client: AsyncClient, job_id: str) -> dict[str, Any]:
    for _ in range(600):
        payload: dict[str, Any] = (
            await client.get(f"/platform/v1/portability/exports/{job_id}")
        ).json()
        if payload["status"] != "running":
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError("export never finished")


async def _applied(client: AsyncClient, job_id: str) -> dict[str, Any]:
    for _ in range(600):
        payload: dict[str, Any] = (
            await client.get(f"/platform/v1/portability/imports/{job_id}")
        ).json()
        if payload["status"] != "running":
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError("import never finished")


# ── export ────────────────────────────────────────────────────────────────────


async def test_export_starts_polls_and_downloads(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    service = await _service(tmp_path, engine)
    try:
        async with _client(service) as client:
            start = await client.post("/platform/v1/portability/exports")
            assert start.status_code == 202
            job_id = start.json()["id"]
            # The plan is complete from the first poll — a progress bar needs a denominator.
            assert {c["kind"] for c in start.json()["progress"]} == {"core", "files"}

            done = await _ready(client, job_id)
            assert done["status"] == "ready", done.get("error")
            assert done["size_bytes"] > 0
            assert done["manifest"]["tenant"] == TENANT

            archive = await client.get(f"/platform/v1/portability/exports/{job_id}/archive")
            assert archive.status_code == 200
            assert archive.headers["content-type"] == "application/gzip"
            assert "epicurus-local-" in archive.headers["content-disposition"]
            path = tmp_path / "downloaded.tar.gz"
            path.write_bytes(archive.content)
            with tarfile.open(path, "r:gz") as tar:
                assert "manifest.json" in tar.getnames()
                assert "files/notes/a.md" in tar.getnames()
    finally:
        await engine.dispose()


async def test_an_unknown_or_foreign_job_is_a_404(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    service = await _service(tmp_path, engine)
    try:
        async with _client(service) as client:
            assert (await client.get("/platform/v1/portability/exports/nope")).status_code == 404
            job_id = (await client.post("/platform/v1/portability/exports")).json()["id"]
            await _ready(client, job_id)
            # Another tenant's view of the same id: absent, not forbidden.
            foreign = await client.get(
                f"/platform/v1/portability/exports/{job_id}", params={"tenant_id": "other"}
            )
            assert foreign.status_code == 404
            # And an export id is not an import id.
            assert (
                await client.get(f"/platform/v1/portability/imports/{job_id}")
            ).status_code == 404
    finally:
        await engine.dispose()


async def test_downloading_a_swept_archive_is_a_410_not_a_500(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    service = await _service(tmp_path, engine)
    try:
        async with _client(service) as client:
            job_id = (await client.post("/platform/v1/portability/exports")).json()["id"]
            done = await _ready(client, job_id)
            assert done["status"] == "ready"
            job = await service.job(tenant=TENANT, job_id=job_id)
            assert job is not None
            Path(job.archive_path or "").unlink()  # staging is disposable, by design

            gone = await client.get(f"/platform/v1/portability/exports/{job_id}/archive")
            assert gone.status_code == 410
            assert "expired" in gone.json()["detail"]
    finally:
        await engine.dispose()


# ── import ────────────────────────────────────────────────────────────────────


async def test_upload_previews_without_applying_then_apply_reports(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    service = await _service(tmp_path, engine)
    try:
        async with _client(service) as client:
            export_id = (await client.post("/platform/v1/portability/exports")).json()["id"]
            await _ready(client, export_id)
            archive = (
                await client.get(f"/platform/v1/portability/exports/{export_id}/archive")
            ).content

            staged = await client.post(
                "/platform/v1/portability/imports",
                files={"file": ("epicurus.tar.gz", archive, "application/gzip")},
            )
            assert staged.status_code == 200
            preview = staged.json()["preview"]
            assert staged.json()["status"] == "staged"
            assert preview["compatible"] is True
            assert preview["manifest"]["tenant"] == TENANT
            assert {c["name"] for c in preview["components"]} >= {"conversations", "files"}
            assert all(c["verdict"] == "ok" for c in preview["components"])
            # Nothing applied yet — no report.
            assert staged.json()["report"] is None

            job_id = staged.json()["id"]
            applied = await client.post(f"/platform/v1/portability/imports/{job_id}/apply")
            assert applied.status_code == 202

            done = await _applied(client, job_id)
            assert done["status"] == "done", done.get("error")
            assert done["report"]["files"]["skipped"] == 1  # identical bytes, left alone
            assert done["report"]["files"]["conflicts"] == []
    finally:
        await engine.dispose()


async def test_applying_twice_is_refused_the_second_time(tmp_path: Path) -> None:
    """An apply is a one-shot on a staged job; re-uploading is the way to do it again."""
    engine = await _engine(tmp_path)
    service = await _service(tmp_path, engine)
    try:
        async with _client(service) as client:
            export_id = (await client.post("/platform/v1/portability/exports")).json()["id"]
            await _ready(client, export_id)
            archive = (
                await client.get(f"/platform/v1/portability/exports/{export_id}/archive")
            ).content
            job_id = (
                await client.post(
                    "/platform/v1/portability/imports",
                    files={"file": ("a.tar.gz", archive, "application/gzip")},
                )
            ).json()["id"]
            await client.post(f"/platform/v1/portability/imports/{job_id}/apply")
            await _applied(client, job_id)

            again = await client.post(f"/platform/v1/portability/imports/{job_id}/apply")
            assert again.status_code == 409
            assert "not staged" in again.json()["detail"]
    finally:
        await engine.dispose()


async def test_an_unreadable_upload_is_a_400(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    service = await _service(tmp_path, engine)
    try:
        async with _client(service) as client:
            response = await client.post(
                "/platform/v1/portability/imports",
                files={"file": ("junk.tar.gz", b"not a tarball at all", "application/gzip")},
            )
            assert response.status_code == 400
            assert "unreadable archive" in response.json()["detail"]
    finally:
        await engine.dispose()


async def test_an_oversized_upload_is_a_413(tmp_path: Path) -> None:
    engine = await _engine(tmp_path)
    service = await _service(tmp_path, engine)
    try:
        async with _client(service, max_archive_bytes=16) as client:
            response = await client.post(
                "/platform/v1/portability/imports",
                files={"file": ("big.tar.gz", b"x" * 4096, "application/gzip")},
            )
            assert response.status_code == 413
    finally:
        await engine.dispose()


async def test_an_incompatible_archive_cannot_be_applied(tmp_path: Path) -> None:
    """The format-version refusal is enforced, not merely displayed."""
    import io
    import json

    from epicurus_core_app.portability.models import PORTABILITY_FORMAT_VERSION

    engine = await _engine(tmp_path)
    service = await _service(tmp_path, engine)
    archive = tmp_path / "future.tar.gz"
    payload = json.dumps(
        {
            "format_version": PORTABILITY_FORMAT_VERSION + 1,
            "tenant": TENANT,
            "created_at": "2026-09-04T09:00:00+00:00",
            "core_app_version": "9.0.0",
            "epicurus_core_version": "9.0.0",
            "components": [],
            "exclusions": [],
            "secrets": {"provider_keys": [], "connected_accounts": []},
        }
    ).encode()
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    try:
        async with _client(service) as client:
            staged = await client.post(
                "/platform/v1/portability/imports",
                files={"file": ("future.tar.gz", archive.read_bytes(), "application/gzip")},
            )
            assert staged.json()["preview"]["compatible"] is False
            job_id = staged.json()["id"]

            refused = await client.post(f"/platform/v1/portability/imports/{job_id}/apply")
            assert refused.status_code == 409
            assert "format version" in refused.json()["detail"]
    finally:
        await engine.dispose()
