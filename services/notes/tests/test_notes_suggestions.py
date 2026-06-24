"""Tests for the notes suggestion queue + the agent's write-only tool surface (#KB-refactor).

Notes are private: the agent proposes changes (create/edit/append/delete) staged for review,
can list titles, but never reads a body.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import PlatformClient
from epicurus_core.contracts import ToolEnvelope
from epicurus_notes.db import NotesStore
from epicurus_notes.pages import NotesPages
from epicurus_notes.service import build_module
from epicurus_notes.suggestions import (
    NoteSuggestionReview,
    NoteSuggestionStore,
    validate_note_operation,
)

TENANT = "test"


def _module_for(store: NotesStore, sugg: NoteSuggestionStore, *, review_on: bool = True):  # type: ignore[no-untyped-def]
    """Build the notes module + its fake indexer, with review on or off (#KB-refactor)."""
    indexer = _FakeIndexer()
    pages = NotesPages(store, indexer, tenant=TENANT)  # type: ignore[arg-type]
    review = NoteSuggestionReview(sugg, pages, store, tenant=TENANT)
    platform = AsyncMock(spec=PlatformClient)
    platform.get_suggestions_enabled = AsyncMock(return_value=review_on)
    return build_module(store, sugg, review, platform, tenant=TENANT), indexer


class _FakeIndexer:
    def __init__(self) -> None:
        self.indexed: list[str] = []
        self.deleted: list[str] = []

    async def index_note(self, slug: str, content: str) -> int:
        self.indexed.append(slug)
        return 1

    async def delete_note(self, slug: str) -> None:
        self.deleted.append(slug)


async def _stores() -> tuple[NotesStore, NoteSuggestionStore]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = NotesStore(engine)
    await store.init()
    sugg = NoteSuggestionStore(engine)
    await sugg.init()
    return store, sugg


async def _review(
    store: NotesStore, sugg: NoteSuggestionStore
) -> tuple[NoteSuggestionReview, _FakeIndexer]:
    indexer = _FakeIndexer()
    pages = NotesPages(store, indexer, tenant=TENANT)  # type: ignore[arg-type]  # mirror=None
    return NoteSuggestionReview(sugg, pages, store, tenant=TENANT), indexer


def _envelope(content: list) -> ToolEnvelope:  # type: ignore[type-arg]
    return ToolEnvelope.model_validate_json(content[0].text)  # type: ignore[attr-defined]


# ── validate / store ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("op", ["create", "update", "append", "delete"])
def test_validate_accepts_known(op: str) -> None:
    assert validate_note_operation(op) == op


def test_validate_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        validate_note_operation("move")  # notes has no move/folders


async def test_store_roundtrip() -> None:
    _, sugg = await _stores()
    s = await sugg.add(
        tenant=TENANT, slug="a", operation="create", proposed_content="x", origin="agent", note=""
    )
    got = await sugg.get(tenant=TENANT, sid=s.sid)
    assert got is not None and got.slug == "a"
    assert [r.slug for r in await sugg.list(tenant=TENANT)] == ["a"]
    assert await sugg.delete(tenant=TENANT, sid=s.sid) is True


# ── review: diff + apply per operation ─────────────────────────────────────────


async def test_append_diff_and_apply_concatenates() -> None:
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="N", content="line1")
    review, indexer = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT,
        slug="n",
        operation="append",
        proposed_content="line2",
        origin="agent",
        note="",
    )
    rs = (await review.list_review()).suggestions[0]
    assert rs.operation == "append"
    assert rs.current == "line1"
    assert rs.content == "line1\nline2"
    assert "+line2" in rs.diff

    await review.approve(s.sid)
    note = await store.get(tenant=TENANT, slug="n")
    assert note is not None and note.content == "line1\nline2"
    assert "n" in indexer.indexed
    assert await sugg.list(tenant=TENANT) == []


async def test_approve_create_writes_the_note() -> None:
    store, sugg = await _stores()
    review, _ = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT,
        slug="new",
        operation="create",
        proposed_content="# Hi",
        origin="agent",
        note="",
    )
    await review.approve(s.sid)
    note = await store.get(tenant=TENANT, slug="new")
    assert note is not None and note.content == "# Hi"


async def test_approve_update_honours_content_override() -> None:
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="N", content="old")
    review, _ = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT,
        slug="n",
        operation="update",
        proposed_content="full",
        origin="agent",
        note="",
    )
    await review.approve(s.sid, content="merged")  # per-hunk merged result
    note = await store.get(tenant=TENANT, slug="n")
    assert note is not None and note.content == "merged"


async def test_approve_delete_removes_note_and_deindexes() -> None:
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="N", content="bye")
    review, indexer = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT, slug="n", operation="delete", proposed_content="", origin="agent", note=""
    )
    await review.approve(s.sid)
    assert await store.get(tenant=TENANT, slug="n") is None
    assert "n" in indexer.deleted


async def test_reject_keeps_the_note() -> None:
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="N", content="keep")
    review, _ = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT, slug="n", operation="update", proposed_content="x", origin="agent", note=""
    )
    await review.reject(s.sid)
    note = await store.get(tenant=TENANT, slug="n")
    assert note is not None and note.content == "keep"
    assert await sugg.list(tenant=TENANT) == []


# ── the agent tools ────────────────────────────────────────────────────────────


async def test_tool_append_stages_a_suggestion() -> None:
    store, sugg = await _stores()
    module, _ = _module_for(store, sugg)
    content, _ = await module.mcp.call_tool("notes_append", {"slug": "n", "text": "more"})
    env = _envelope(content)
    assert "pending your review" in env.text.lower()
    rows = await sugg.list(tenant=TENANT)
    assert len(rows) == 1
    assert (
        rows[0].operation == "append" and rows[0].slug == "n" and rows[0].proposed_content == "more"
    )


async def test_tool_delete_stages_a_suggestion() -> None:
    store, sugg = await _stores()
    module, _ = _module_for(store, sugg)
    await module.mcp.call_tool("notes_delete", {"slug": "n"})
    rows = await sugg.list(tenant=TENANT)
    assert len(rows) == 1 and rows[0].operation == "delete"


async def test_tool_list_shows_titles_not_bodies() -> None:
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="My Note", content="secret body")
    module, _ = _module_for(store, sugg)
    content, _ = await module.mcp.call_tool("notes_list", {})
    text = content[0].text
    assert "My Note" in text
    assert "secret body" not in text  # privacy: never leaks the body


async def test_no_read_tool_exists() -> None:
    store, sugg = await _stores()
    module, _ = _module_for(store, sugg)
    manifest = await module.manifest()
    names = {t.name for t in manifest.tools}
    assert not any("get" in n or "read" in n for n in names)


async def test_tool_append_auto_applies_when_review_off() -> None:
    # With review off, the agent's change is applied directly — nothing left pending.
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="N", content="a")
    module, _ = _module_for(store, sugg, review_on=False)
    content, _ = await module.mcp.call_tool("notes_append", {"slug": "n", "text": "b"})
    env = _envelope(content)
    assert "applied directly" in env.text.lower()
    note = await store.get(tenant=TENANT, slug="n")
    assert note is not None and note.content == "a\nb"
    assert await sugg.list(tenant=TENANT) == []
