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
ENTRY = "/platform/v1/files/entry"


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

    async def delete(self, *, tenant: str, path: str) -> bool:
        # Remove the entry and its subtree; report whether anything was there (idempotent).
        victims = [p for p in self._tree if p == path or p.startswith(f"{path}/")]
        for p in victims:
            del self._tree[p]
        return bool(victims)


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


async def test_move_operator_file_into_module_dir_is_refused(
    tmp_path: Path, fake: FakeObjects
) -> None:
    # An operator file dropped onto a module folder is refused: writing behind the module's back
    # would desync its index — the same rule the upload 400 already enforces (#479/#554).
    client, _, store = await _make_client(tmp_path, objects=fake)
    async with client:
        resp = await client.post(MOVE, json={"src": "top.txt", "dst": "knowledge/top.txt"})
    assert resp.status_code == 400
    assert "knowledge" in resp.json()["detail"]
    # The guard runs before the seam: the source is untouched and nothing landed in the module.
    assert await store.stat(tenant=TENANT, path="top.txt") is not None
    assert await store.stat(tenant=TENANT, path="knowledge/top.txt") is None


async def test_move_module_self_move_same_top_still_allowed(
    tmp_path: Path, fake: FakeObjects
) -> None:
    # The src-top == dst-top carve-out: a module moving its own file within its subtree is a
    # legitimate operation the module-facing files_move relies on — it must not be blocked.
    client, _, store = await _make_client(tmp_path, objects=fake)
    async with client:
        resp = await client.post(
            MOVE, json={"src": "knowledge/readme.md", "dst": "knowledge/moved.md"}
        )
    assert resp.status_code == 200
    assert await store.stat(tenant=TENANT, path="knowledge/moved.md") is not None


async def test_rename_smuggling_a_path_into_a_module_is_refused(
    tmp_path: Path, fake: FakeObjects
) -> None:
    # Rename is a same-parent move; a "new name" carrying a leading path relocates the file. If
    # the smuggled top is a module, the move guard is the server backstop to the web field (#554).
    client, _, store = await _make_client(tmp_path, objects=fake)
    async with client:
        resp = await client.post(MOVE, json={"src": "top.txt", "dst": "notes/top.txt"})
    assert resp.status_code == 400
    assert "notes" in resp.json()["detail"]
    assert await store.stat(tenant=TENANT, path="top.txt") is not None


async def test_move_pathological_name_is_a_clean_400(tmp_path: Path, fake: FakeObjects) -> None:
    # A control char / NUL or an over-long segment would surface as an OSError 500 from the store;
    # the shared sanitizer turns each into a clean 400 at the door (#554).
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        control = await client.post(MOVE, json={"src": "top.txt", "dst": "bad\tname.txt"})
        nul = await client.post(MOVE, json={"src": "top.txt", "dst": "bad\x00name.txt"})
        too_long = await client.post(MOVE, json={"src": "top.txt", "dst": "z" * 300 + ".txt"})
    assert control.status_code == 400
    assert nul.status_code == 400
    assert too_long.status_code == 400


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


# ── Dedupe of the two-source merge (#560) ────────────────────────────────────────


async def test_page_dedupes_colliding_folder(tmp_path: Path, fake: FakeObjects) -> None:
    # The object store also reports the file space's ``knowledge`` folder at root.
    fake._tree["knowledge"] = ("dir", b"")
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        root = (await client.get(PAGE)).json()
        into = (await client.get(PAGE, params={"path": "knowledge"})).json()
    ids = [it["id"] for it in root["items"]]
    # The colliding folder renders once; the merged root is otherwise intact and stably sorted.
    assert ids.count("knowledge") == 1
    assert [it["title"] for it in root["items"]] == ["knowledge", "notes", "uploads", "top.txt"]
    # Navigating into the collapsed folder still shows the file-space children (unchanged).
    assert {it["id"] for it in into["items"]} == {"knowledge/readme.md"}


async def test_page_dedupes_colliding_file_file_space_wins(
    tmp_path: Path, fake: FakeObjects
) -> None:
    # Both stores report the same locked file. Un-deduped it renders twice, and the object copy
    # (hard-coded ``movable=True``) would override the file space's authoritative read-only (#479).
    fake._tree["knowledge/readme.md"] = ("file", b"obj-copy")
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        into = (await client.get(PAGE, params={"path": "knowledge"})).json()
        search = (await client.get(PAGE, params={"q": "readme"})).json()
    into_rows = [it for it in into["items"] if it["id"] == "knowledge/readme.md"]
    search_rows = [it for it in search["items"] if it["id"] == "knowledge/readme.md"]
    # Browse and search both collapse to one row; file-space precedence keeps it read-only.
    assert len(into_rows) == 1 and into_rows[0]["movable"] is False
    assert len(search_rows) == 1 and search_rows[0]["movable"] is False


async def test_page_dedupes_unlocked_file_stays_movable(tmp_path: Path, fake: FakeObjects) -> None:
    # A collision outside any locked prefix: both sources say movable, so precedence does not
    # change the flag — but the row must still collapse to one (the general dedupe case).
    fake._tree["top.txt"] = ("file", b"obj-copy")
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        root = (await client.get(PAGE)).json()
    rows = [it for it in root["items"] if it["id"] == "top.txt"]
    assert len(rows) == 1 and rows[0]["movable"] is True


async def test_degrades_without_object_backend(tmp_path: Path) -> None:
    client, _, _ = await _make_client(tmp_path, objects=None)
    async with client:
        data = (await client.get(PAGE)).json()
        obj_read = await client.get(READ, params={"path": "uploads/note.txt"})
    # No object backend → the page is the file-space tree alone, object reads 404.
    assert [it["title"] for it in data["items"]] == ["knowledge", "notes", "top.txt"]
    assert obj_read.status_code == 404


# ── The Files-page delete door (#564) ────────────────────────────────────────────


async def test_delete_file_space_file_deindexes(tmp_path: Path, fake: FakeObjects) -> None:
    client, index, store = await _make_client(tmp_path, objects=fake)
    async with client:
        resp = await client.request("DELETE", ENTRY, params={"path": "top.txt"})
        gone = (await client.get(SEARCH, params={"q": "top"})).json()["entries"]
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    # Gone from disk and, immediately, from the index/search — no rescan needed (#390 is backup).
    assert await store.stat(tenant=TENANT, path="top.txt") is None
    assert await index.get(tenant=TENANT, path="top.txt") is None
    assert gone == []


async def test_delete_folder_removes_subtree_recursively(tmp_path: Path, fake: FakeObjects) -> None:
    client, index, store = await _make_client(tmp_path, objects=fake)
    async with client:
        for name, body in (("a.txt", b"aa"), ("b.txt", b"bb")):
            await client.post(
                UPLOAD, params={"dir": "reports"}, files={"file": (name, body, "text/plain")}
            )
        resp = await client.request("DELETE", ENTRY, params={"path": "reports"})
        left = (await client.get(SEARCH, params={"q": "reports"})).json()["entries"]
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    # The whole subtree is gone on disk and de-indexed — the recursion is real (#564 acceptance).
    assert await store.stat(tenant=TENANT, path="reports") is None
    assert await index.get(tenant=TENANT, path="reports/a.txt") is None
    assert await index.get(tenant=TENANT, path="reports/b.txt") is None
    assert left == []


async def test_delete_object_via_fallback(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        resp = await client.request("DELETE", ENTRY, params={"path": "uploads/note.txt"})
    # Not in the file space → the delete falls through to the object store (symmetric to move).
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    assert "uploads/note.txt" not in fake._tree


async def test_delete_missing_is_deleted_false(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        resp = await client.request("DELETE", ENTRY, params={"path": "ghost.txt"})
    # Nothing in either store — a clean False, not a 404 (idempotent, like the seam).
    assert resp.status_code == 200 and resp.json()["deleted"] is False


async def test_delete_module_subtree_is_refused(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, store = await _make_client(tmp_path, objects=fake)
    async with client:
        # A crafted request against a module file, and against the module folder itself.
        file_resp = await client.request("DELETE", ENTRY, params={"path": "knowledge/readme.md"})
        dir_resp = await client.request("DELETE", ENTRY, params={"path": "knowledge"})
    assert file_resp.status_code == 400 and "knowledge module" in file_resp.json()["detail"]
    assert dir_resp.status_code == 400
    # The guard runs before the seam — the module's file is untouched.
    assert await store.stat(tenant=TENANT, path="knowledge/readme.md") is not None


async def test_delete_root_is_400(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        resp = await client.request("DELETE", ENTRY, params={"path": ""})
    assert resp.status_code == 400


async def test_delete_object_path_without_backend_is_false(tmp_path: Path) -> None:
    client, _, _ = await _make_client(tmp_path, objects=None)
    async with client:
        resp = await client.request("DELETE", ENTRY, params={"path": "uploads/note.txt"})
    # No object backend and not in the file space → no fallback, a clean False.
    assert resp.status_code == 200 and resp.json()["deleted"] is False


async def test_page_marks_deletable(tmp_path: Path, fake: FakeObjects) -> None:
    client, _, _ = await _make_client(tmp_path, objects=fake)
    async with client:
        data = (await client.get(PAGE)).json()
    by_name = {it["title"]: it for it in data["items"]}
    # Module-owned subtrees are not deletable from the Files UI; operator files/objects are —
    # including directories, unlike movability (#564).
    assert by_name["knowledge"]["deletable"] is False
    assert by_name["notes"]["deletable"] is False
    assert by_name["top.txt"]["deletable"] is True
    assert by_name["uploads"]["deletable"] is True
