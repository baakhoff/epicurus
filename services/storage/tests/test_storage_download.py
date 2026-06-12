"""Integration tests for the /download HTTP endpoint (no Postgres needed)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


@pytest.fixture
def file_tree(tmp_path: Path) -> Path:
    (tmp_path / "hello.txt").write_text("hello world")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("nested")
    return tmp_path


@pytest.fixture
def storage_app(file_tree: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("STORAGE_ROOT", str(file_tree))
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
