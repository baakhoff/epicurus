"""Tests for the core-owned file-space API (ADR-0052), ``/platform/v1/files``.

Exercised against a real ``LocalFileStore`` over a tmp dir through the ASGI app — no DB,
no lifespan, no MinIO. Confirms the contract the modules consume via ``PlatformClient``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from epicurus_core.files import LocalFileStore
from epicurus_core_app.files_routes import create_files_router

TENANT = "local"

WRITE = "/platform/v1/files/write"
READ = "/platform/v1/files/read"
LIST = "/platform/v1/files/list"
STAT = "/platform/v1/files/stat"
DIR = "/platform/v1/files/dir"
ROOT = "/platform/v1/files"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(create_files_router(LocalFileStore(tmp_path), default_tenant=TENANT))
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as c:
        yield c


async def test_write_list_read_round_trip(client: AsyncClient) -> None:
    w = await client.put(WRITE, params={"path": "docs/a.md"}, json={"content": "hello"})
    assert w.status_code == 200
    assert w.json()["path"] == "docs/a.md"

    ls = await client.get(LIST, params={"path": ""})
    assert ls.status_code == 200
    assert any(e["name"] == "docs" and e["kind"] == "dir" for e in ls.json()["entries"])

    rd = await client.get(READ, params={"path": "docs/a.md"})
    assert rd.status_code == 200
    assert rd.json() == {"path": "docs/a.md", "name": "a.md", "content": "hello"}


async def test_read_missing_is_404(client: AsyncClient) -> None:
    resp = await client.get(READ, params={"path": "nope.txt"})
    assert resp.status_code == 404


@pytest.mark.parametrize("endpoint", [READ, LIST, STAT])
async def test_traversal_is_400(client: AsyncClient, endpoint: str) -> None:
    resp = await client.get(endpoint, params={"path": "../escape"})
    assert resp.status_code == 400


async def test_stat_then_delete(client: AsyncClient) -> None:
    await client.put(WRITE, params={"path": "f.txt"}, json={"content": "x"})
    stat = await client.get(STAT, params={"path": "f.txt"})
    assert stat.status_code == 200 and stat.json()["kind"] == "file"

    delete = await client.request("DELETE", ROOT, params={"path": "f.txt"})
    assert delete.status_code == 200 and delete.json()["deleted"] is True
    assert (await client.get(STAT, params={"path": "f.txt"})).status_code == 404


async def test_make_dir(client: AsyncClient) -> None:
    resp = await client.post(DIR, params={"path": "projects"})
    assert resp.status_code == 200 and resp.json()["kind"] == "dir"


async def test_read_too_large_is_413(client: AsyncClient) -> None:
    big = "x" * (256 * 1024 + 1)
    await client.put(WRITE, params={"path": "big.txt"}, json={"content": big})
    resp = await client.get(READ, params={"path": "big.txt"})
    assert resp.status_code == 413


async def test_write_and_delete_root_are_400(client: AsyncClient) -> None:
    assert (await client.put(WRITE, params={"path": ""}, json={"content": "x"})).status_code == 400
    assert (await client.request("DELETE", ROOT, params={"path": ""})).status_code == 400


async def test_tenant_isolation(client: AsyncClient) -> None:
    await client.put(
        WRITE, params={"path": "secret.txt", "tenant_id": "tenant-a"}, json={"content": "a"}
    )
    ls = await client.get(LIST, params={"path": "", "tenant_id": "tenant-b"})
    assert ls.json()["entries"] == []
