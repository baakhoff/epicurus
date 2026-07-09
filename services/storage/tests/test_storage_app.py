"""Integration tests for the storage ASGI app (object surface + ingest), no infra needed.

After the file-space migration (ADR-0063) the app exposes the **object surface** the core's
Files view proxies — ``/objects`` (browse/search), ``/objects/read``, ``/download``,
``/objects/move`` — plus the ``/ingest`` chat-upload sink. There is no longer a ``/pages/...``
route, a filesystem ``/read``, or a filesystem ``/download``: the core owns the file space.

The app is exercised over ``httpx.ASGITransport`` (which does **not** run the lifespan, so no
NATS/MinIO connect happens). To give the routes a working index + object store without real
infra, ``FileIndex`` and ``ObjectStore`` are monkeypatched in the app module to capture
in-memory instances the test seeds directly.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import route_paths
from epicurus_storage.db import FileIndex
from epicurus_storage.object_store import ObjectStore, StoredObject

TENANT = "local"


class _MemObjectStore(ObjectStore):
    """In-memory object store used by the app under test — no MinIO."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(url="http://unused", access_key="x", secret_key="x")
        self._mem: dict[str, StoredObject] = {}

    def _k(self, tenant: str, key: str) -> str:
        return f"{tenant}\x00{key}"

    async def put_bytes(
        self, *, tenant: str, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        self._mem[self._k(tenant, key)] = StoredObject(data=data, content_type=content_type)

    async def get_object(self, *, tenant: str, key: str) -> StoredObject | None:
        return self._mem.get(self._k(tenant, key))

    async def put(self, *, tenant: str, key: str, content: str) -> None:
        await self.put_bytes(
            tenant=tenant, key=key, data=content.encode("utf-8"), content_type="text/plain"
        )

    async def get(self, *, tenant: str, key: str) -> str | None:
        stored = self._mem.get(self._k(tenant, key))
        return None if stored is None else stored.data.decode("utf-8")

    async def copy(self, *, tenant: str, src_key: str, dst_key: str) -> None:
        self._mem[self._k(tenant, dst_key)] = self._mem[self._k(tenant, src_key)]

    async def delete(self, *, tenant: str, key: str) -> None:
        self._mem.pop(self._k(tenant, key), None)


@dataclass
class _Harness:
    app: object
    index: FileIndex
    objects: _MemObjectStore


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Harness]:
    """Build the app with an in-memory index + object store the test can seed.

    A shared SQLite engine is created up front and ``init()``-ed (the lifespan that would
    normally do this is not run under ASGITransport); the app is patched to reuse the same
    index and a fake object store.
    """
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("DEFAULT_TENANT_ID", TENANT)
    monkeypatch.setenv("PLATFORM_URL", "http://core-app:8080")

    import epicurus_storage.app as amod
    import epicurus_storage.settings as smod

    importlib.reload(smod)
    importlib.reload(amod)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    index = FileIndex(engine)
    await index.init()
    objects = _MemObjectStore()

    monkeypatch.setattr(amod, "FileIndex", lambda _engine: index)
    monkeypatch.setattr(amod, "ObjectStore", lambda **_kwargs: objects)

    app = amod.create_app()
    try:
        yield _Harness(app=app, index=index, objects=objects)
    finally:
        await engine.dispose()


async def _seed_object(h: _Harness, key: str, content: str) -> None:
    """Catalogue an object + its bytes the way ``put_object`` would (so it is browsable)."""
    from epicurus_storage.service import put_object

    await put_object(index=h.index, objects=h.objects, tenant=TENANT, key=key, content=content)


def _client(h: _Harness) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=h.app), base_url="http://test")  # type: ignore[arg-type]


# ── Route surface ───────────────────────────────────────────────────────────────


def test_app_exposes_the_object_surface(harness: _Harness) -> None:
    paths = route_paths(harness.app)  # type: ignore[arg-type]
    assert "/health" in paths
    assert "/metrics" in paths
    assert "/manifest" in paths
    assert "/ingest" in paths
    assert "/objects" in paths
    assert "/objects/read" in paths
    assert "/download" in paths
    assert "/objects/move" in paths
    assert any(p.startswith("/mcp") for p in paths)
    # The file-space routes are gone — the core owns them now.
    assert "/read" not in paths
    assert "/pages/{page_id}" not in paths
    assert "/pages/{page_id}/move" not in paths


# ── /objects (browse + search) ──────────────────────────────────────────────────


async def test_objects_browse_root(harness: _Harness) -> None:
    await _seed_object(harness, "uploads/a.md", "a")
    await _seed_object(harness, "b.md", "b")
    async with _client(harness) as client:
        resp = await client.get("/objects")
    assert resp.status_code == 200
    names = {e["name"] for e in resp.json()["entries"]}
    assert names == {"uploads", "b.md"}


async def test_objects_browse_subdir(harness: _Harness) -> None:
    await _seed_object(harness, "uploads/inner.md", "x")
    async with _client(harness) as client:
        resp = await client.get("/objects", params={"path": "uploads"})
    entries = resp.json()["entries"]
    assert [(e["name"], e["kind"]) for e in entries] == [("inner.md", "file")]


async def test_objects_search_overrides_path(harness: _Harness) -> None:
    await _seed_object(harness, "reports/quarterly.md", "x")
    await _seed_object(harness, "misc/other.md", "y")
    async with _client(harness) as client:
        resp = await client.get("/objects", params={"q": "quarterly"})
    names = {e["name"] for e in resp.json()["entries"]}
    assert names == {"quarterly.md"}


# ── /objects/read ───────────────────────────────────────────────────────────────


async def test_objects_read_returns_text(harness: _Harness) -> None:
    await _seed_object(harness, "memo.md", "hello world")
    async with _client(harness) as client:
        resp = await client.get("/objects/read", params={"path": "memo.md"})
    assert resp.status_code == 200
    assert resp.json() == {"path": "memo.md", "name": "memo.md", "content": "hello world"}


async def test_objects_read_missing_is_404(harness: _Harness) -> None:
    async with _client(harness) as client:
        resp = await client.get("/objects/read", params={"path": "nope.md"})
    assert resp.status_code == 404


async def test_objects_read_binary_is_415(harness: _Harness) -> None:
    await harness.objects.put_bytes(
        tenant=TENANT, key="blob.bin", data=b"\xff\xfe\x00", content_type="application/octet-stream"
    )
    await harness.index.upsert_batch(
        tenant=TENANT,
        source="object",
        entries=[{"path": "blob.bin", "name": "blob.bin", "size": 3, "mtime": 0.0, "kind": "file"}],
    )
    async with _client(harness) as client:
        resp = await client.get("/objects/read", params={"path": "blob.bin"})
    assert resp.status_code == 415


async def test_objects_read_too_large_is_413(harness: _Harness) -> None:
    big = b"x" * (256 * 1024 + 1)
    await harness.objects.put_bytes(
        tenant=TENANT, key="big.txt", data=big, content_type="text/plain"
    )
    await harness.index.upsert_batch(
        tenant=TENANT,
        source="object",
        entries=[
            {"path": "big.txt", "name": "big.txt", "size": len(big), "mtime": 0.0, "kind": "file"}
        ],
    )
    async with _client(harness) as client:
        resp = await client.get("/objects/read", params={"path": "big.txt"})
    assert resp.status_code == 413


# ── /download (object hit + 404 miss) ───────────────────────────────────────────


async def test_download_serves_a_catalogued_object(harness: _Harness) -> None:
    await _seed_object(harness, "report.md", "agent bytes")
    async with _client(harness) as client:
        resp = await client.get("/download", params={"path": "report.md"})
    assert resp.status_code == 200
    assert resp.content == b"agent bytes"
    assert 'filename="report.md"' in resp.headers["content-disposition"]


async def test_download_missing_object_is_404(harness: _Harness) -> None:
    async with _client(harness) as client:
        resp = await client.get("/download", params={"path": "ghost.md"})
    assert resp.status_code == 404


async def test_download_does_not_fall_back_to_filesystem(harness: _Harness) -> None:
    # A path that is not a catalogued object 404s — there is no filesystem fallback anymore.
    await harness.index.upsert_batch(
        tenant=TENANT,
        source="fs",  # a (hypothetical) file-space row, NOT an object
        entries=[{"path": "fs.txt", "name": "fs.txt", "size": 1, "mtime": 0.0, "kind": "file"}],
    )
    async with _client(harness) as client:
        resp = await client.get("/download", params={"path": "fs.txt"})
    assert resp.status_code == 404


# ── /objects/move ───────────────────────────────────────────────────────────────


async def test_move_renames_an_object(harness: _Harness) -> None:
    await _seed_object(harness, "notes/draft.md", "hi")
    async with _client(harness) as client:
        resp = await client.post(
            "/objects/move", json={"from_path": "notes/draft.md", "to_path": "notes/final.md"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"path": "notes/final.md"}
    assert await harness.objects.get(tenant=TENANT, key="notes/final.md") == "hi"


async def test_move_missing_source_is_404(harness: _Harness) -> None:
    async with _client(harness) as client:
        resp = await client.post("/objects/move", json={"from_path": "ghost.md", "to_path": "x.md"})
    assert resp.status_code == 404


# ── DELETE /objects (the core's Files-page delete fallback, #564) ─────────────────


async def test_delete_removes_an_object(harness: _Harness) -> None:
    await _seed_object(harness, "uploads/gone.md", "bye")
    async with _client(harness) as client:
        resp = await client.request("DELETE", "/objects", params={"path": "uploads/gone.md"})
    assert resp.status_code == 200 and resp.json() == {"deleted": True}
    assert await harness.objects.get(tenant=TENANT, key="uploads/gone.md") is None
    assert await harness.index.get(tenant=TENANT, path="uploads/gone.md") is None


async def test_delete_missing_is_deleted_false(harness: _Harness) -> None:
    async with _client(harness) as client:
        resp = await client.request("DELETE", "/objects", params={"path": "ghost.md"})
    assert resp.status_code == 200 and resp.json() == {"deleted": False}


async def test_delete_root_is_400(harness: _Harness) -> None:
    async with _client(harness) as client:
        resp = await client.request("DELETE", "/objects", params={"path": ""})
    assert resp.status_code == 400


# ── /ingest (chat upload sink, ADR-0025) ────────────────────────────────────────


async def test_ingest_stores_and_catalogues_an_upload(harness: _Harness) -> None:
    async with _client(harness) as client:
        resp = await client.post(
            "/ingest",
            params={"filename": "report.pdf", "att_id": "abc123"},
            content=b"%PDF-1.4 fake",
            headers={"content-type": "application/pdf"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"key": "uploads/abc123-report.pdf", "name": "report.pdf", "size": 13}
    # It is now browsable + downloadable through the object surface.
    async with _client(harness) as client:
        listed = await client.get("/objects", params={"path": "uploads"})
        dl = await client.get("/download", params={"path": "uploads/abc123-report.pdf"})
    assert {e["name"] for e in listed.json()["entries"]} == {"report.pdf"}
    assert dl.status_code == 200
    assert dl.content == b"%PDF-1.4 fake"


# ── Settings ────────────────────────────────────────────────────────────────────


def test_settings_platform_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLATFORM_URL", "http://core-app:8080")
    import epicurus_storage.settings as smod

    importlib.reload(smod)
    s = smod.StorageSettings(service_name="storage")
    assert s.platform_url == "http://core-app:8080"
    # The removed filesystem setting is truly gone.
    assert not hasattr(s, "storage_root")
