"""Tests for the notes .md mirror into the shared file space (#KB-refactor, req 7).

Postgres stays the source of truth; the mirror is best-effort read-only output so the
storage module shows notes in the Files view.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_notes.db import NotesStore
from epicurus_notes.indexer import NotesIndexer
from epicurus_notes.mirror import NotesMirror
from epicurus_notes.pages import NotesPages

TENANT = "test"


async def _store() -> NotesStore:
    store = NotesStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    return store


async def test_write_creates_md_file(tmp_path: Path) -> None:
    mirror = NotesMirror(tmp_path, await _store(), tenant=TENANT)
    await mirror.write("my-note", "# Hi\nbody")
    assert (tmp_path / "my-note.md").read_text(encoding="utf-8") == "# Hi\nbody"


async def test_write_rejects_traversal_slug(tmp_path: Path) -> None:
    mirror = NotesMirror(tmp_path, await _store(), tenant=TENANT)
    await mirror.write("../escape", "nope")  # never raises — skips the unsafe slug
    assert not (tmp_path.parent / "escape.md").exists()


async def test_write_does_not_double_suffix_md(tmp_path: Path) -> None:
    # A slug authored via the editor's file controls already ends in ".md" (#KB-refactor);
    # the mirror must write "<name>.md", never "<name>.md.md".
    mirror = NotesMirror(tmp_path, await _store(), tenant=TENANT)
    await mirror.write("work/idea.md", "body")
    assert (tmp_path / "work" / "idea.md").read_text(encoding="utf-8") == "body"
    assert not (tmp_path / "work" / "idea.md.md").exists()


async def test_backfill_writes_only_missing(tmp_path: Path) -> None:
    store = await _store()
    await store.upsert(tenant=TENANT, slug="a", title="A", content="aaa")
    await store.upsert(tenant=TENANT, slug="b", title="B", content="bbb")
    # A pre-existing mirror for "a" must not be clobbered (saves keep it current).
    (tmp_path / "a.md").write_text("stale", encoding="utf-8")
    mirror = NotesMirror(tmp_path, store, tenant=TENANT)

    written = await mirror.backfill()

    assert written == 1  # only "b" was missing
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "stale"
    assert (tmp_path / "b.md").read_text(encoding="utf-8") == "bbb"


async def test_write_doc_mirrors_the_saved_body(tmp_path: Path) -> None:
    store = await _store()
    indexer = AsyncMock(spec=NotesIndexer)
    indexer.index_note = AsyncMock(return_value=1)
    mirror = NotesMirror(tmp_path, store, tenant=TENANT)
    pages = NotesPages(store, indexer, tenant=TENANT, mirror=mirror)

    await pages.write_doc("hello", "# Hello\n")

    assert (tmp_path / "hello.md").read_text(encoding="utf-8") == "# Hello\n"
