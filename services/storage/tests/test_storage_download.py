"""Integration tests for the /download HTTP endpoint (no Postgres needed)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


# The served tree is tenant-scoped (constraint #1): STORAGE_ROOT is /data (the base) and
# the app serves /data/<tenant>. The default tenant is "local", so the sample files live
# under <root>/local — that subtree is what /read and /download confine to.
TENANT = "local"


@pytest.fixture
def file_tree(tmp_path: Path) -> Path:
    served = tmp_path / TENANT
    served.mkdir()
    (served / "hello.txt").write_text("hello world")
    (served / "sub").mkdir()
    (served / "sub" / "nested.txt").write_text("nested")
    return tmp_path


@pytest.fixture
def storage_app(file_tree: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    # STORAGE_ROOT is the base (/data); the app appends the tenant segment itself.
    monkeypatch.setenv("STORAGE_ROOT", str(file_tree))
    monkeypatch.setenv("DEFAULT_TENANT_ID", TENANT)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    import importlib

    import epicurus_storage.app as amod
    import epicurus_storage.settings as smod

    importlib.reload(smod)
    importlib.reload(amod)
    return amod.create_app()


@pytest.mark.anyio
async def test_download_existing_file(storage_app: object, file_tree: Path) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/download", params={"path": "hello.txt"})
    assert resp.status_code == 200
    assert resp.content == b"hello world"


@pytest.mark.anyio
async def test_download_missing_file(storage_app: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/download", params={"path": "nope.txt"})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_download_path_traversal_rejected(storage_app: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/download", params={"path": "../../etc/passwd"})
    assert resp.status_code in {400, 404}


@pytest.mark.anyio
async def test_download_directory_rejected(storage_app: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/download", params={"path": "sub"})
    assert resp.status_code == 400


# ── /pages/{page_id} ─────────────────────────────────────────────────────────
# The 404 guard fires before any DB access, so ASGITransport (no lifespan) is fine.
# Data-shape correctness is covered by the unit tests in test_storage.py via
# build_page_data() directly.


# ── /read (split-screen reader, #KB-refactor req 6) ──────────────────────────


@pytest.mark.anyio
async def test_read_returns_text(storage_app: object, file_tree: Path) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/read", params={"path": "hello.txt"})
    assert resp.status_code == 200
    assert resp.json() == {"path": "hello.txt", "name": "hello.txt", "content": "hello world"}


@pytest.mark.anyio
async def test_read_missing_is_404(storage_app: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/read", params={"path": "nope.txt"})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_read_traversal_rejected(storage_app: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/read", params={"path": "../../etc/passwd"})
    assert resp.status_code in {400, 404}


@pytest.mark.anyio
async def test_read_directory_rejected(storage_app: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/read", params={"path": "sub"})
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_read_binary_rejected(storage_app: object, file_tree: Path) -> None:
    # Written into the tenant subtree — that is the served root the route confines to.
    (file_tree / TENANT / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/read", params={"path": "blob.bin"})
    assert resp.status_code == 415


@pytest.mark.anyio
async def test_download_confines_to_tenant_subtree(storage_app: object, file_tree: Path) -> None:
    # A file that exists at the STORAGE_ROOT base but OUTSIDE the tenant subtree must not be
    # reachable: the served root is /data/<tenant>, so the base-level file is invisible (404).
    (file_tree / "outside.txt").write_text("not yours")
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/download", params={"path": "outside.txt"})
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_pages_unknown_id_returns_404(storage_app: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/pages/nonexistent")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_pages_404_for_second_unknown(storage_app: object) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=storage_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp = await client.get("/pages/not-a-real-page-id")
    assert resp.status_code == 404
