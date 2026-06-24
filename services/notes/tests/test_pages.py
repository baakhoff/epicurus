"""Tests for the editor page surface (#134): list, read, create, save.

Uses the real Postgres-backed store on in-memory SQLite; the vector indexer is
faked so these tests exercise the document contract and slug-safety boundary, not
embeddings or Qdrant.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_notes.db import NoteFolderStore, NotesStore
from epicurus_notes.pages import NotesPages, create_pages_router, derive_title

TENANT = "test"


class _FakeIndexer:
    """Records index_note/delete_note calls; optionally raises to simulate an embed failure."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.deleted: list[str] = []
        self._fail = fail

    async def index_note(self, slug: str, content: str) -> int:
        self.calls.append(slug)
        if self._fail:
            raise RuntimeError("embed unavailable")
        return 2

    async def delete_note(self, slug: str) -> None:
        self.deleted.append(slug)


async def _store() -> NotesStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = NotesStore(engine)
    await store.init()
    return store


async def _pages_with_folders(indexer: _FakeIndexer | None = None) -> NotesPages:
    """A NotesPages backed by both a notes store and a folder store, sharing one engine."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store, folders = NotesStore(engine), NoteFolderStore(engine)
    await store.init()
    await folders.init()
    return NotesPages(store, indexer or _FakeIndexer(), tenant=TENANT, folders=folders)


# ── title derivation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("# Hello\n\nbody", "Hello"),
        ("plain first line\nmore", "plain first line"),
        ("\n\n## Sub heading\n", "Sub heading"),
        ("", "Untitled"),
        ("###", "Untitled"),
        ("   \n   \n", "Untitled"),
    ],
)
def test_derive_title(content: str, expected: str) -> None:
    assert derive_title(content) == expected


# ── NotesPages behaviour ──────────────────────────────────────────────────────


async def test_list_empty_and_can_manage_files() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    data = await pages.list_docs()
    assert data.docs == []
    assert data.can_manage_files is True
    assert data.title == "Notes"


async def test_write_creates_note_and_indexes() -> None:
    store = await _store()
    indexer = _FakeIndexer()
    saved: list[str] = []

    async def on_saved(slug: str) -> None:
        saved.append(slug)

    pages = NotesPages(store, indexer, tenant=TENANT, on_saved=on_saved)
    result = await pages.write_doc("my-note", "# My Note\n\nbody")

    assert result.path == "my-note"
    assert result.indexed is True
    assert result.chunk_count == 2
    assert indexer.calls == ["my-note"]
    assert saved == ["my-note"]
    # The note is now listable, titled from its first heading.
    doc = await pages.read_doc("my-note")
    assert doc.title == "My Note"
    assert doc.content == "# My Note\n\nbody"
    listed = await pages.list_docs()
    assert [d.path for d in listed.docs] == ["my-note"]
    assert listed.docs[0].title == "My Note"


async def test_write_updates_existing_note() -> None:
    store = await _store()
    pages = NotesPages(store, _FakeIndexer(), tenant=TENANT)
    await pages.write_doc("n", "# One")
    await pages.write_doc("n", "# Two\n\nmore")
    doc = await pages.read_doc("n")
    assert doc.title == "Two"
    assert (await pages.list_docs()).docs.__len__() == 1


async def test_write_saves_even_when_index_fails() -> None:
    # The note is the source of truth — a failed embed must not lose it.
    store = await _store()
    pages = NotesPages(store, _FakeIndexer(fail=True), tenant=TENANT)
    result = await pages.write_doc("n", "kept")
    assert result.indexed is False
    assert (await pages.read_doc("n")).content == "kept"


async def test_read_missing_is_404() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    with pytest.raises(HTTPException) as err:
        await pages.read_doc("ghost")
    assert err.value.status_code == 404


@pytest.mark.parametrize("bad", ["", "   ", " leading", "trailing ", "ctrl\nchar"])
async def test_invalid_slugs_rejected_without_writing(bad: str) -> None:
    store = await _store()
    pages = NotesPages(store, _FakeIndexer(), tenant=TENANT)
    with pytest.raises(HTTPException) as err:
        await pages.write_doc(bad, "x")
    assert err.value.status_code == 400
    assert await store.count(tenant=TENANT) == 0


# ── router (the HTTP surface the core proxies) ────────────────────────────────


def _client(pages: NotesPages) -> TestClient:
    app = FastAPI()
    app.include_router(create_pages_router(pages))
    return TestClient(app)


async def test_router_create_read_list_roundtrip() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    client = _client(pages)

    save = client.put("/pages/notes/doc", params={"path": "idea"}, json={"content": "# Idea"})
    assert save.status_code == 200
    assert save.json()["indexed"] is True

    listed = client.get("/pages/notes")
    assert listed.status_code == 200
    body = listed.json()
    assert body["can_manage_files"] is True
    assert [d["path"] for d in body["docs"]] == ["idea"]

    read = client.get("/pages/notes/doc", params={"path": "idea"})
    assert read.json()["title"] == "Idea"


async def test_router_unknown_page_is_404() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    assert _client(pages).get("/pages/ghost").status_code == 404


async def test_router_invalid_slug_is_400() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    resp = _client(pages).get("/pages/notes/doc", params={"path": "   "})
    assert resp.status_code == 400


# ── folders (#KB-refactor) ─────────────────────────────────────────────────────


async def test_empty_folder_persists_as_dir_node() -> None:
    pages = await _pages_with_folders()
    await pages.create_folder("work")
    data = await pages.list_docs()
    dirs = [(d.path, d.type) for d in data.docs if d.type == "dir"]
    assert dirs == [("work", "dir")]


async def test_nested_note_implies_ancestor_dirs_before_files() -> None:
    # A slug with "/" implies its folders; dirs must be emitted before files and
    # parent-before-child so the shell's tree builder can attach each node.
    pages = await _pages_with_folders()
    await pages.write_doc("a/b/note", "# Deep")
    paths = [(d.path, d.type) for d in (await pages.list_docs()).docs]
    assert paths == [("a", "dir"), ("a/b", "dir"), ("a/b/note", "file")]


async def test_create_duplicate_folder_is_409() -> None:
    pages = await _pages_with_folders()
    await pages.create_folder("dup")
    with pytest.raises(HTTPException) as err:
        await pages.create_folder("dup")
    assert err.value.status_code == 409


@pytest.mark.parametrize("bad", ["", "  ", "/", "a/../b", "a//b", "."])
async def test_create_folder_rejects_invalid_paths(bad: str) -> None:
    pages = await _pages_with_folders()
    with pytest.raises(HTTPException) as err:
        await pages.create_folder(bad)
    assert err.value.status_code == 400


async def test_delete_empty_folder_succeeds() -> None:
    pages = await _pages_with_folders()
    await pages.create_folder("temp")
    await pages.delete_folder("temp")
    assert (await pages.list_docs()).docs == []


async def test_delete_folder_with_a_note_is_409() -> None:
    pages = await _pages_with_folders()
    await pages.create_folder("keep")
    await pages.write_doc("keep/note", "x")
    with pytest.raises(HTTPException) as err:
        await pages.delete_folder("keep")
    assert err.value.status_code == 409


async def test_delete_folder_with_a_child_folder_is_409() -> None:
    pages = await _pages_with_folders()
    await pages.create_folder("parent")
    await pages.create_folder("parent/child")
    with pytest.raises(HTTPException) as err:
        await pages.delete_folder("parent")
    assert err.value.status_code == 409


async def test_delete_missing_folder_is_404() -> None:
    pages = await _pages_with_folders()
    with pytest.raises(HTTPException) as err:
        await pages.delete_folder("ghost")
    assert err.value.status_code == 404


# ── move / rename (#KB-refactor) ───────────────────────────────────────────────


async def test_move_rekeys_note_and_reindexes() -> None:
    indexer = _FakeIndexer()
    pages = await _pages_with_folders(indexer)
    await pages.write_doc("old", "# Title\n\nbody")
    result = await pages.move_item("old", "new")
    assert result == {"path": "new"}
    assert (await pages.read_doc("new")).content == "# Title\n\nbody"
    with pytest.raises(HTTPException) as err:
        await pages.read_doc("old")
    assert err.value.status_code == 404
    assert "new" in indexer.calls and "old" in indexer.deleted


async def test_move_into_a_folder() -> None:
    pages = await _pages_with_folders()
    await pages.write_doc("note", "# N")
    await pages.move_item("note", "work/note")
    paths = [(d.path, d.type) for d in (await pages.list_docs()).docs]
    assert paths == [("work", "dir"), ("work/note", "file")]


async def test_move_missing_source_is_404() -> None:
    pages = await _pages_with_folders()
    with pytest.raises(HTTPException) as err:
        await pages.move_item("ghost", "x")
    assert err.value.status_code == 404


async def test_move_to_existing_destination_is_409() -> None:
    pages = await _pages_with_folders()
    await pages.write_doc("a", "A")
    await pages.write_doc("b", "B")
    with pytest.raises(HTTPException) as err:
        await pages.move_item("a", "b")
    assert err.value.status_code == 409


# ── router (folder/move/delete HTTP surface) ───────────────────────────────────


async def test_router_folder_and_move_roundtrip() -> None:
    pages = await _pages_with_folders()
    client = _client(pages)

    assert client.post("/pages/notes/folder", params={"path": "proj"}).status_code == 200
    client.put("/pages/notes/doc", params={"path": "proj/n"}, json={"content": "# N"})
    moved = client.post(
        "/pages/notes/move", json={"from_path": "proj/n", "to_path": "proj/renamed"}
    )
    assert moved.status_code == 200
    assert moved.json()["path"] == "proj/renamed"

    # The doc under the folder moved; the folder still lists.
    paths = {d["path"] for d in client.get("/pages/notes").json()["docs"]}
    assert {"proj", "proj/renamed"} <= paths

    # Delete the doc, then the now-empty folder.
    assert client.delete("/pages/notes/doc", params={"path": "proj/renamed"}).status_code == 204
    assert client.delete("/pages/notes/folder", params={"path": "proj"}).status_code == 204
    assert client.get("/pages/notes").json()["docs"] == []


# ── version history (ADR-0046) ────────────────────────────────────────────────


async def test_editor_data_is_versioned() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    data = await pages.list_docs()
    assert data.versioned is True


async def test_write_records_versions_newest_first() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    await pages.write_doc("n", "# One")
    await pages.write_doc("n", "# Two")

    listed = await pages.list_versions("n")
    assert [v.title for v in listed.versions] == ["Two", "One"]
    assert all(v.size > 0 for v in listed.versions)


async def test_write_dedups_identical_resave() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    await pages.write_doc("n", "# Same")
    await pages.write_doc("n", "# Same")
    assert len((await pages.list_versions("n")).versions) == 1


async def test_write_still_versions_when_index_fails() -> None:
    # The save committed even though the embed failed, so the snapshot must exist.
    pages = NotesPages(await _store(), _FakeIndexer(fail=True), tenant=TENANT)
    result = await pages.write_doc("n", "kept")
    assert result.indexed is False
    assert len((await pages.list_versions("n")).versions) == 1


async def test_get_version_returns_content_then_404_for_unknown() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    await pages.write_doc("n", "# Body\n\ntext")
    [summary] = (await pages.list_versions("n")).versions

    fetched = await pages.get_version("n", summary.version_id)
    assert fetched.content == "# Body\n\ntext"
    assert fetched.version_id == summary.version_id
    assert fetched.path == "n"

    with pytest.raises(HTTPException) as err:
        await pages.get_version("n", "404404")
    assert err.value.status_code == 404
    with pytest.raises(HTTPException) as garbage:
        await pages.get_version("n", "not-an-int")
    assert garbage.value.status_code == 404


async def test_version_router_roundtrip() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    client = _client(pages)
    client.put("/pages/notes/doc", params={"path": "idea"}, json={"content": "# First"})
    client.put("/pages/notes/doc", params={"path": "idea"}, json={"content": "# Second"})

    versions = client.get("/pages/notes/doc/versions", params={"path": "idea"})
    assert versions.status_code == 200
    body = versions.json()["versions"]
    assert [v["title"] for v in body] == ["Second", "First"]

    newest_id = body[0]["version_id"]
    fetched = client.get("/pages/notes/doc/version", params={"path": "idea", "version": newest_id})
    assert fetched.status_code == 200
    assert fetched.json()["content"] == "# Second"

    missing = client.get("/pages/notes/doc/version", params={"path": "idea", "version": "999999"})
    assert missing.status_code == 404


async def test_version_router_unknown_page_is_404() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    resp = _client(pages).get("/pages/ghost/doc/versions", params={"path": "x"})
    assert resp.status_code == 404


async def test_versions_are_tenant_isolated() -> None:
    store = await _store()
    pages_a = NotesPages(store, _FakeIndexer(), tenant="tenant-a")
    pages_b = NotesPages(store, _FakeIndexer(), tenant="tenant-b")
    await pages_a.write_doc("shared", "# A only")
    [v] = (await pages_a.list_versions("shared")).versions

    # tenant-b sees none of tenant-a's versions and cannot fetch one by id.
    assert (await pages_b.list_versions("shared")).versions == []
    with pytest.raises(HTTPException) as err:
        await pages_b.get_version("shared", v.version_id)
    assert err.value.status_code == 404
