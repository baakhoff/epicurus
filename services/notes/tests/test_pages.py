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

from epicurus_notes.db import NotesStore
from epicurus_notes.pages import NotesPages, create_pages_router, derive_title

TENANT = "test"


class _FakeIndexer:
    """Records index_note calls; optionally raises to simulate an embed failure."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self._fail = fail

    async def index_note(self, slug: str, content: str) -> int:
        self.calls.append(slug)
        if self._fail:
            raise RuntimeError("embed unavailable")
        return 2

    async def delete_note(self, slug: str) -> None:  # pragma: no cover - unused here
        pass


async def _store() -> NotesStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = NotesStore(engine)
    await store.init()
    return store


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


async def test_list_empty_and_can_create_true() -> None:
    pages = NotesPages(await _store(), _FakeIndexer(), tenant=TENANT)
    data = await pages.list_docs()
    assert data.docs == []
    assert data.can_create is True
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
    assert body["can_create"] is True
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
