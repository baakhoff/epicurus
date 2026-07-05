"""Tests for the core Files UI endpoints (ADR-0063): page / search / read / download / move.

Exercised against a real ``LocalFileStore`` + a real ``FileIndex`` (in-memory SQLite) and a
**fake** object backend, through the ASGI app — proving the unified view (file space merged with
the object store), the object fallback on read/download/move, and graceful degrade when no object
backend is wired.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core.files import LocalFileStore
from epicurus_core_app.file_index import FileIndex
from epicurus_core_app.file_scan import scan
from epicurus_core_app.object_backend import ObjectDownload, ObjectEntry, ObjectText

TENANT = "local"
PAGE = "/platform/v1/files/page"
SEARCH = "/platform/v1/files/search"
READ = "/platform/v1/files/read"
DOWNLOAD = "/platform/v1/files/download"
MOVE = "/platform/v1/files/move"
UPLOAD = "/platform/v1/files/upload"


class FakeObjects:
    """An in-memory object backend mirroring storage's object surface for the merge tests."""

    def __init__(self) -> None:
        # path -> (kind, bytes); a small "uploads/" subtree of objects.
        self._tree: dict[str, tuple[str, bytes]] = {
            "uploads": ("dir", b""),
            "uploads/note.txt": ("file", b"obj-body"),
        }
        self.down = False

    async def list(self, *, tenant: str, path: str, query: str) -> list[ObjectEntry]:
        if self.down:
            return []
        if query:
            return [
                ObjectEntry(path=p, name=p.rsplit("/", 1)[-1], kind=k, size=len(c))
                for p, (k, c) in self._tree.items()
                if k == "file" and query.lower() in p.lower()
            ]
        prefix = f"{path}/" if path else ""
        out: list[ObjectEntry] = []
        for p, (k, c) in self._tree.items():
            rel = p[len(prefix) :]
            if p.startswith(prefix) and rel and "/" not in rel:
                out.append(ObjectEntry(path=p, name=rel, kind=k, size=len(c)))
        return out

    async def read(self, *, tenant: str, path: str) -> ObjectText | None:
        item = self._tree.get(path)
        if item is None or item[0] != "file":
            return None
        return ObjectText(path=path, name=path.rsplit("/", 1)[-1], content=item[1].decode())

    async def download(self, *, tenant: str, path: str) -> ObjectDownload | None:
        item = self._tree.get(path)
        if item is None or item[0] != "file":
            return None

        async def _gen() -> AsyncIterator[bytes]:
            yield item[1]

        return ObjectDownload(name=path.rsplit("/", 1)[-1], content_type="text/plain", body=_gen())

    async def move(self, *, tenant: str, src: str, dst: str) -> ObjectEntry:
        kind, body = self._tree.pop(src)
        self._tree[dst] = (kind, body)
        return ObjectEntry(path=dst, name=dst.rsplit("/", 1)[-1], kind=kind, size=len(body))


def _tree(root: Path) -> None:
    base = root / TENANT
    (base / "knowledge").mkdir(parents=True)
    (base / "knowledge" / "readme.md").write_text("hello", encoding="utf-8")
    (base / "notes").mkdir()
    (base / "notes" / "todo.md").write_text("x", encoding="utf-8")
    (base / "top.txt").write_text("abc", encoding="utf-8")


async def _make_client(
    tmp_path: Path, *, objects: FakeObjects | None
) -> tuple[AsyncClient, FileIndex, LocalFileStore]:
    _tree(tmp_path)
    store = LocalFileStore(tmp_path)
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    index = FileIndex(engine)
    await index.init()
    await scan(store, index, tenant=TENANT)
    from epicurus_core_app.files_routes import create_files_router

    app = FastAPI()
    app.include_router(
        create_files_router(
            store,
            default_tenant=TENANT,
            index=index,
            objects=objects,
            # Production locks each module's top-level folder (settings.module_hostnames);
            # mirror that so movability and the upload guard assert the real rule (#479).
            locked_prefixes=frozenset({"knowledge", "notes"}),
        )
    )
    client = AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    )
    return client, index, store


@pytest.fixture
async def fake() -> FakeObjects:
    return FakeObjects()


async def test_page_root_merges_file_space_and_objects(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        data = (await client.get(PAGE)).json()
    assert data["title"] == "Files"
    assert data["search_enabled"] is True
    names = [it["title"] for it in data["items"]]
    # Dirs first (file-space knowledge/notes + object uploads), then the file.
    assert names == ["knowledge", "notes", "uploads", "top.txt"]
    by_name = {it["title"]: it for it in data["items"]}
    # Module subtrees and directories are read-only in the UI; object entries and
    # operator-space files are movable (#479).
    assert by_name["knowledge"]["movable"] is False
    assert by_name["uploads"]["movable"] is True
    assert by_name["top.txt"]["movable"] is True
    # Files carry a core download href; directories do not.
    assert by_name["top.txt"]["href"] == "/platform/v1/files/download?path=top.txt"
    assert by_name["knowledge"]["href"] is None


async def test_page_into_object_dir(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        data = (await client.get(PAGE, params={"path": "uploads"})).json()
    items = {it["title"]: it for it in data["items"]}
    assert set(items) == {"note.txt"}
    assert items["note.txt"]["movable"] is True


async def test_page_search_spans_both(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        readme = (await client.get(PAGE, params={"q": "readme"})).json()
        note = (await client.get(PAGE, params={"q": "note.txt"})).json()
    # "readme" hits only the file space; "note.txt" only the object store — both surface.
    assert {it["id"] for it in readme["items"]} == {"knowledge/readme.md"}
    assert {it["id"] for it in note["items"]} == {"uploads/note.txt"}


async def test_search_endpoint_is_file_space_only(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        hits = (await client.get(SEARCH, params={"q": "todo"})).json()["entries"]
    assert [h["path"] for h in hits] == ["notes/todo.md"]


async def test_read_file_space_then_object(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        fs = await client.get(READ, params={"path": "knowledge/readme.md"})
        obj = await client.get(READ, params={"path": "uploads/note.txt"})
        missing = await client.get(READ, params={"path": "nope.txt"})
    assert fs.json()["content"] == "hello"
    assert obj.json() == {"path": "uploads/note.txt", "name": "note.txt", "content": "obj-body"}
    assert missing.status_code == 404


async def test_download_file_space_then_object(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        fs = await client.get(DOWNLOAD, params={"path": "top.txt"})
        obj = await client.get(DOWNLOAD, params={"path": "uploads/note.txt"})
        missing = await client.get(DOWNLOAD, params={"path": "nope.txt"})
    assert fs.status_code == 200 and fs.content == b"abc"
    assert "attachment" in fs.headers["content-disposition"]
    assert obj.status_code == 200 and obj.content == b"obj-body"
    assert missing.status_code == 404


async def test_move_object_via_fallback(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        resp = await client.post(
            MOVE, json={"src": "uploads/note.txt", "dst": "uploads/renamed.txt"}
        )
    assert resp.status_code == 200 and resp.json()["path"] == "uploads/renamed.txt"
    assert "uploads/renamed.txt" in fake._tree and "uploads/note.txt" not in fake._tree


async def test_move_file_space_updates_index(tmp_path: Path, fake: FakeObjects) -> None:
    client, index, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        resp = await client.post(
            MOVE, json={"src": "knowledge/readme.md", "dst": "knowledge/readme2.md"}
        )
    assert resp.status_code == 200 and resp.json()["path"] == "knowledge/readme2.md"
    # The index is updated immediately so the moved file is findable without waiting for a rescan.
    assert await index.get(tenant=TENANT, path="knowledge/readme2.md") is not None


async def test_upload_is_indexed_and_listed_immediately(tmp_path: Path, fake: FakeObjects) -> None:
    client, index, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        up = await client.post(
            UPLOAD, params={"dir": "inbox"}, files={"file": ("report.txt", b"q3", "text/plain")}
        )
        assert up.status_code == 200 and up.json()["path"] == "inbox/report.txt"
        # Listed in its directory, movable (operator space), downloadable — no rescan needed.
        page = (await client.get(PAGE, params={"path": "inbox"})).json()
        hits = (await client.get(SEARCH, params={"q": "report"})).json()["entries"]
    items = {it["title"]: it for it in page["items"]}
    assert items["report.txt"]["movable"] is True
    assert items["report.txt"]["href"] == "/platform/v1/files/download?path=inbox/report.txt"
    assert [h["path"] for h in hits] == ["inbox/report.txt"]
    assert await index.get(tenant=TENANT, path="inbox/report.txt") is not None


async def test_search_marks_module_files_read_only(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        page = (await client.get(PAGE, params={"q": "readme"})).json()
    items = {it["id"]: it for it in page["items"]}
    # A module-owned file surfaces in search but stays read-only (#479).
    assert items["knowledge/readme.md"]["movable"] is False


async def test_degrades_without_object_backend(tmp_path: Path) -> None:
    client, _, _ = await _make_client(tmp_path, objects=None)
    async with client:
        data = (await client.get(PAGE)).json()
        obj_read = await client.get(READ, params={"path": "uploads/note.txt"})
    # No object backend → the page is the file-space tree alone, object reads 404.
    assert [it["title"] for it in data["items"]] == ["knowledge", "notes", "top.txt"]
    assert obj_read.status_code == 404
