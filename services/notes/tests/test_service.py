"""Manifest + tool-surface tests for the notes module.

Notes are **private**: the agent may list titles and *propose* changes, but has **no
read/get tool** for a note's body (#KB-refactor). It exposes the editor + review pages and
the attach surface.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import EpicurusModule, PlatformClient
from epicurus_notes.db import NotesStore
from epicurus_notes.events import NOTE_CREATED, NOTE_DELETED, NOTE_UPDATED
from epicurus_notes.indexer import NotesIndexer
from epicurus_notes.pages import NotesPages
from epicurus_notes.service import build_module
from epicurus_notes.suggestions import (
    NoteSuggestionAuditStore,
    NoteSuggestionReview,
    NoteSuggestionStore,
)


def _module() -> EpicurusModule:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # manifest()/tool listing don't touch the DB, so uninitialised stores are fine here.
    store, sugg = NotesStore(engine), NoteSuggestionStore(engine)
    pages = NotesPages(store, AsyncMock(spec=NotesIndexer), tenant="test")
    audit = NoteSuggestionAuditStore(engine)  # never exercised (no approve/reject here)
    review = NoteSuggestionReview(sugg, pages, store, tenant="test", audit=audit)
    platform = AsyncMock(spec=PlatformClient)
    platform.get_suggestions_enabled = AsyncMock(return_value=True)
    return build_module(store, sugg, review, platform, tenant="test")


async def test_manifest_identity() -> None:
    manifest = await _module().manifest()
    assert manifest.name == "notes"
    assert manifest.version == "0.9.0"


async def test_exposes_write_and_list_tools_but_no_read() -> None:
    manifest = await _module().manifest()
    names = {t.name for t in manifest.tools}
    assert {
        "notes_list",
        "notes_tree",
        "notes_create",
        "notes_propose_edit",
        "notes_append",
        "notes_delete",
    } <= names
    # Notes are private: there must be NO tool that returns a note's body.
    assert not any("get" in n or "read" in n for n in names)


async def test_is_attachable() -> None:
    manifest = await _module().manifest()
    assert manifest.attachable is True


async def test_declares_editor_and_review_pages() -> None:
    manifest = await _module().manifest()
    by_id = {p.id: p for p in manifest.pages}
    assert by_id["notes"].archetype == "editor"
    assert by_id["notes"].title == "Notes"
    assert by_id["review"].archetype == "review"


async def test_has_ui_and_declares_spine_events() -> None:
    manifest = await _module().manifest()
    assert manifest.ui is not None
    assert manifest.ui.status_url == "/status"
    subjects = {e.subject for e in manifest.events_emitted}
    # Spine events (#665) — the legacy bare `notes.saved` declaration is gone.
    expected = {f"events.{t}" for t in (NOTE_CREATED, NOTE_UPDATED, NOTE_DELETED)}
    assert expected <= subjects
    assert "notes.saved" not in subjects


async def test_full_body_writes_open_the_document_pane() -> None:
    """The two tools whose `content` is the note's whole body carry the annotation (#541)."""
    tools = {t.name: t for t in (await _module().manifest()).tools}

    for name in ("notes_create", "notes_propose_edit"):
        annotation = tools[name].writes_document
        assert annotation is not None, name
        assert annotation.content_arg == "content"
        assert annotation.target_arg == "slug"
        # The body is the title's source (the module derives it), so there is no title arg.
        assert annotation.title_arg is None


async def test_append_and_delete_do_not_open_the_document_pane() -> None:
    """`notes_append`'s `text` is a fragment, not a document — showing it as one would lie.

    The agent cannot read a note (they're private), so append supplies only what to add and the
    server concatenates it on approval. `notes_delete` has no body at all.
    """
    tools = {t.name: t for t in (await _module().manifest()).tools}
    assert tools["notes_append"].writes_document is None
    assert tools["notes_delete"].writes_document is None
    assert tools["notes_list"].writes_document is None
