"""HTTP tests for the object-serving routes — /download and /objects/read.

These confine to the response wiring: the routes resolve a path to a catalogued object via
``load_object_download`` and stream / return it, or 404 when it is not one. The resolver is
stubbed so no index schema, MinIO, or app lifespan is needed (ASGITransport does not run the
lifespan). End-to-end coverage against a real in-memory index lives in test_storage_app.py;
the catalogue plumbing lives in test_storage_ingest.py.

After the file-space migration (ADR-0063) there is **no** filesystem ``/download`` or ``/read``:
the core owns the file space, and these routes only serve catalogued objects.
"""

from __future__ import annotations

import importlib
import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

TENANT = "local"


@pytest.fixture
def storage_app(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("DEFAULT_TENANT_ID", TENANT)

    import epicurus_storage.app as amod
    import epicurus_storage.settings as smod

    importlib.reload(smod)
    importlib.reload(amod)
    return amod.create_app()


def _stub_object(monkeypatch: pytest.MonkeyPatch, key: str, data: bytes, content_type: str) -> None:
    """Patch the app's object resolver so only *key* resolves to an object download."""
    import epicurus_storage.app as amod
    from epicurus_storage.service import ObjectDownload

    name = key.rsplit("/", 1)[-1]

    async def fake_load(*, index: object, objects: object, tenant: str, path: str) -> object:
        if path == key:
            return ObjectDownload(name=name, data=data, content_type=content_type)
        return None

    monkeypatch.setattr(amod, "load_object_download", fake_load)


# ── /download ───────────────────────────────────────────────────────────────────


async def test_download_serves_an_object(
    storage_app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_object(monkeypatch, "report.md", b"agent bytes", "text/plain")
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/download", params={"path": "report.md"})
    assert resp.status_code == 200
    assert resp.content == b"agent bytes"
    assert resp.headers["content-type"].startswith("text/plain")
    assert 'filename="report.md"' in resp.headers["content-disposition"]


async def test_download_serves_an_object_under_a_nested_key(
    storage_app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Agent objects are not confined to uploads/ — any catalogued key serves.
    _stub_object(monkeypatch, "reports/2026/q2.bin", b"\x00\x01\x02", "application/octet-stream")
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/download", params={"path": "reports/2026/q2.bin"})
    assert resp.status_code == 200
    assert resp.content == b"\x00\x01\x02"


async def test_download_missing_is_404(
    storage_app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_object(monkeypatch, "exists.md", b"x", "text/plain")
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/download", params={"path": "nope.md"})
    assert resp.status_code == 404


# ── /objects/read ───────────────────────────────────────────────────────────────


async def test_read_object_returns_text(
    storage_app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_object(monkeypatch, "report.md", b"agent text", "text/plain")
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/objects/read", params={"path": "report.md"})
    assert resp.status_code == 200
    assert resp.json() == {"path": "report.md", "name": "report.md", "content": "agent text"}


async def test_read_object_missing_is_404(
    storage_app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_object(monkeypatch, "exists.md", b"x", "text/plain")
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/objects/read", params={"path": "nope.md"})
    assert resp.status_code == 404


async def test_read_object_binary_is_415(
    storage_app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_object(monkeypatch, "blob.bin", b"\xff\xfe\x00", "application/octet-stream")
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/objects/read", params={"path": "blob.bin"})
    assert resp.status_code == 415
