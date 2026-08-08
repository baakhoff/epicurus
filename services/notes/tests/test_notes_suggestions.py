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
    MAX_DECISIONS,
    NoteSuggestionAuditStore,
    NoteSuggestionReview,
    NoteSuggestionStore,
    validate_note_operation,
)

TENANT = "test"


async def _audit_store() -> NoteSuggestionAuditStore:
    store = NoteSuggestionAuditStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    return store


async def _module_for(store: NotesStore, sugg: NoteSuggestionStore, *, review_on: bool = True):  # type: ignore[no-untyped-def]
    """Build the notes module + its fake indexer, with review on or off (#KB-refactor)."""
    indexer = _FakeIndexer()
    pages = NotesPages(store, indexer, tenant=TENANT)  # type: ignore[arg-type]
    review = NoteSuggestionReview(sugg, pages, store, tenant=TENANT, audit=await _audit_store())
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
    review = NoteSuggestionReview(sugg, pages, store, tenant=TENANT, audit=await _audit_store())
    return review, indexer


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
    module, _ = await _module_for(store, sugg)
    content, _ = await module.call_tool("notes_append", {"slug": "n", "text": "more"})
    env = _envelope(content)
    assert "pending your review" in env.text.lower()
    rows = await sugg.list(tenant=TENANT)
    assert len(rows) == 1
    assert (
        rows[0].operation == "append" and rows[0].slug == "n" and rows[0].proposed_content == "more"
    )


async def test_tool_delete_stages_a_suggestion() -> None:
    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    await module.call_tool("notes_delete", {"slug": "n"})
    rows = await sugg.list(tenant=TENANT)
    assert len(rows) == 1 and rows[0].operation == "delete"


async def test_tool_list_shows_titles_not_bodies() -> None:
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="My Note", content="secret body")
    module, _ = await _module_for(store, sugg)
    content, _ = await module.call_tool("notes_list", {})
    text = content[0].text
    assert "My Note" in text
    assert "secret body" not in text  # privacy: never leaks the body


async def test_no_note_body_read_tool_exists() -> None:
    """Notes stay private: no tool may read a note's *stored* body.

    ``notes_read_suggestion`` (#744) is a deliberate, narrow exception to the name-shaped
    check below — it echoes back only the agent's own pending proposal (content the agent
    itself supplied, never yet applied to a note), never a note's stored body, so it does
    not violate the invariant this test protects. See its own coverage in
    ``test_read_suggestion_tool_never_reads_a_notes_stored_body``.
    """
    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    manifest = await module.manifest()
    names = {t.name for t in manifest.tools} - {"notes_read_suggestion"}
    assert not any("get" in n or "read" in n for n in names)


async def test_tool_append_auto_applies_when_review_off() -> None:
    # With review off, the agent's change is applied directly — nothing left pending.
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="N", content="a")
    module, _ = await _module_for(store, sugg, review_on=False)
    content, _ = await module.call_tool("notes_append", {"slug": "n", "text": "b"})
    env = _envelope(content)
    assert "applied directly" in env.text.lower()
    note = await store.get(tenant=TENANT, slug="n")
    assert note is not None and note.content == "a\nb"
    assert await sugg.list(tenant=TENANT) == []


# ── resolved-decision audit trail (ADR-0090, #542) ─────────────────────────────


async def test_approve_records_audit_decision_with_edited_content() -> None:
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
    await review.approve(s.sid, content="merged")
    audit = (await review.list_audit()).decisions
    assert len(audit) == 1
    assert audit[0].decision == "approved"
    assert audit[0].proposed_content == "full"
    assert audit[0].applied_content == "merged"


async def test_reject_records_audit_decision_with_no_applied_content() -> None:
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="N", content="keep")
    review, _ = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT, slug="n", operation="update", proposed_content="x", origin="agent", note=""
    )
    await review.reject(s.sid)
    audit = (await review.list_audit()).decisions
    assert len(audit) == 1
    assert audit[0].decision == "rejected"
    assert audit[0].proposed_content == "x"
    assert audit[0].applied_content == ""


async def test_delete_approve_records_audit_with_no_content() -> None:
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="N", content="bye")
    review, _ = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT, slug="n", operation="delete", proposed_content="", origin="agent", note=""
    )
    await review.approve(s.sid)
    audit = (await review.list_audit()).decisions
    assert audit[0].decision == "approved"
    assert audit[0].applied_content == ""


async def test_notes_audit_retention_caps_at_max_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    import epicurus_notes.suggestions as suggestions_module

    monkeypatch.setattr(suggestions_module, "MAX_DECISIONS", 3)
    audit_store = await _audit_store()
    for i in range(5):
        await audit_store.record(
            tenant=TENANT,
            sid=f"s{i}",
            slug=f"n{i}",
            operation="create",
            origin="agent",
            note="",
            proposed_at=datetime.now(UTC),
            decision="approved",
            proposed_content=str(i),
            applied_content=str(i),
        )
    rows = await audit_store.list(tenant=TENANT, limit=10)
    assert len(rows) == 3
    assert [r.id for r in rows] == ["s4", "s3", "s2"]


def test_notes_default_max_decisions_is_200() -> None:
    assert MAX_DECISIONS == 200


# ── suggestion lifecycle: read/update/withdraw (#744) ─────────────────────────


async def test_store_update_revises_content_and_note() -> None:
    _, sugg = await _stores()
    s = await sugg.add(
        tenant=TENANT,
        slug="n",
        operation="update",
        proposed_content="old",
        origin="agent",
        note="why",
    )
    before = await sugg.get(tenant=TENANT, sid=s.sid)
    assert before is not None
    updated = await sugg.update(tenant=TENANT, sid=s.sid, proposed_content="new", note="revised")
    assert updated is not None
    assert updated.proposed_content == "new"
    assert updated.note == "revised"
    assert updated.created_at >= before.created_at  # "proposed-at refreshes"


async def test_store_update_leaves_omitted_fields_unchanged() -> None:
    _, sugg = await _stores()
    s = await sugg.add(
        tenant=TENANT,
        slug="n",
        operation="update",
        proposed_content="keep-me",
        origin="agent",
        note="keep-note",
    )
    updated = await sugg.update(tenant=TENANT, sid=s.sid, note="only note changes")
    assert updated is not None
    assert updated.proposed_content == "keep-me"
    assert updated.note == "only note changes"


async def test_store_update_unknown_sid_returns_none() -> None:
    _, sugg = await _stores()
    assert await sugg.update(tenant=TENANT, sid="nope", note="x") is None


async def test_audit_get_returns_none_for_unknown_sid() -> None:
    audit_store = await _audit_store()
    assert await audit_store.get(tenant=TENANT, sid="nope") is None


async def test_audit_get_finds_a_recorded_decision() -> None:
    from datetime import UTC, datetime

    audit_store = await _audit_store()
    await audit_store.record(
        tenant=TENANT,
        sid="s1",
        slug="n",
        operation="create",
        origin="agent",
        note="",
        proposed_at=datetime.now(UTC),
        decision="rejected",
        proposed_content="a",
        applied_content="",
    )
    got = await audit_store.get(tenant=TENANT, sid="s1")
    assert got is not None and got.decision == "rejected"


async def test_review_read_returns_full_suggestion() -> None:
    store, sugg = await _stores()
    review, _ = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT,
        slug="n",
        operation="create",
        proposed_content="draft body",
        origin="agent",
        note="rationale",
    )
    got = await review.read(s.sid)
    assert got.sid == s.sid
    assert got.proposed_content == "draft body"
    assert got.note == "rationale"


async def test_review_read_unknown_404s() -> None:
    from fastapi import HTTPException

    store, sugg = await _stores()
    review, _ = await _review(store, sugg)
    with pytest.raises(HTTPException) as err:
        await review.read("nope")
    assert err.value.status_code == 404
    assert "no such suggestion" in str(err.value.detail).lower()


async def test_review_read_resolved_404s_with_the_verdict() -> None:
    """A caller can tell 'never existed' from 'already resolved' (#744, #697 precedent)."""
    from fastapi import HTTPException

    store, sugg = await _stores()
    review, _ = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT, slug="n", operation="create", proposed_content="x", origin="agent", note=""
    )
    await review.approve(s.sid)
    with pytest.raises(HTTPException) as err:
        await review.read(s.sid)
    assert err.value.status_code == 404
    assert "already approved" in str(err.value.detail).lower()


async def test_review_update_revises_and_stays_pending() -> None:
    store, sugg = await _stores()
    review, _ = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT,
        slug="n",
        operation="create",
        proposed_content="draft v1",
        origin="agent",
        note="",
    )
    updated = await review.update(s.sid, content="draft v2")
    assert updated.proposed_content == "draft v2"
    pending = await sugg.list(tenant=TENANT)
    assert len(pending) == 1  # still one queue entry, never two
    assert pending[0].sid == s.sid


async def test_review_update_already_withdrawn_404s_with_the_verdict() -> None:
    from fastapi import HTTPException

    store, sugg = await _stores()
    review, _ = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT, slug="n", operation="create", proposed_content="x", origin="agent", note=""
    )
    await review.withdraw(s.sid)
    with pytest.raises(HTTPException) as err:
        await review.update(s.sid, content="too late")
    assert err.value.status_code == 404
    assert "already withdrawn" in str(err.value.detail).lower()


async def test_review_withdraw_removes_from_pending_keeps_history_never_touches_note() -> None:
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="N", content="untouched")
    review, indexer = await _review(store, sugg)
    s = await sugg.add(
        tenant=TENANT,
        slug="n",
        operation="update",
        proposed_content="stale",
        origin="agent",
        note="",
    )
    result = await review.withdraw(s.sid)
    assert result.status == "withdrawn"
    assert await sugg.list(tenant=TENANT) == []
    assert indexer.indexed == [] and indexer.deleted == []
    note = await store.get(tenant=TENANT, slug="n")
    assert note is not None and note.content == "untouched"
    audit = (await review.list_audit()).decisions
    assert audit[0].decision == "withdrawn"
    assert audit[0].proposed_content == "stale"


async def test_review_withdraw_unknown_404s() -> None:
    from fastapi import HTTPException

    store, sugg = await _stores()
    review, _ = await _review(store, sugg)
    with pytest.raises(HTTPException) as err:
        await review.withdraw("nope")
    assert err.value.status_code == 404


# ── the suggestion-lifecycle MCP tools (#744) ─────────────────────────────────


async def test_list_suggestions_tool_shows_pending() -> None:
    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    await module.call_tool("notes_create", {"slug": "n", "content": "x", "note": "first"})
    content, _ = await module.call_tool("notes_list_suggestions", {})
    text = content[0].text
    assert "n" in text
    assert "first" in text


async def test_list_suggestions_tool_empty_queue() -> None:
    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    content, _ = await module.call_tool("notes_list_suggestions", {})
    assert "no suggestions" in content[0].text.lower()


async def test_list_suggestions_tool_rejects_non_pending_status() -> None:
    from epicurus_core import ToolError

    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    with pytest.raises(ToolError, match=r"(?i)status must be"):
        await module.call_tool("notes_list_suggestions", {"status": "approved"})


async def test_read_suggestion_tool_returns_the_proposal() -> None:
    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    await module.call_tool("notes_create", {"slug": "n", "content": "draft body"})
    sid = (await sugg.list(tenant=TENANT))[0].sid
    content, _ = await module.call_tool("notes_read_suggestion", {"id": sid})
    text = content[0].text
    assert "n" in text
    assert "draft body" in text


async def test_read_suggestion_tool_never_reads_a_notes_stored_body() -> None:
    """The privacy boundary this tool must never cross: only the agent's own draft, never
    the note's current stored content — even when one already exists at the same slug."""
    store, sugg = await _stores()
    await store.upsert(tenant=TENANT, slug="n", title="N", content="PRIVATE STORED BODY")
    module, _ = await _module_for(store, sugg)
    await module.call_tool(
        "notes_propose_edit", {"slug": "n", "content": "agent's proposed replacement"}
    )
    sid = (await sugg.list(tenant=TENANT))[0].sid
    content, _ = await module.call_tool("notes_read_suggestion", {"id": sid})
    text = content[0].text
    assert "agent's proposed replacement" in text
    assert "PRIVATE STORED BODY" not in text


async def test_read_suggestion_tool_unknown_id_raises() -> None:
    from epicurus_core import ToolError

    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    with pytest.raises(ToolError, match=r"(?i)no such suggestion"):
        await module.call_tool("notes_read_suggestion", {"id": "nope"})


async def test_update_suggestion_tool_revises_in_place() -> None:
    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    await module.call_tool("notes_create", {"slug": "n", "content": "draft v1"})
    sid = (await sugg.list(tenant=TENANT))[0].sid
    content, _ = await module.call_tool(
        "notes_update_suggestion", {"id": sid, "content": "draft v2"}
    )
    assert "revised" in content[0].text.lower()
    rows = await sugg.list(tenant=TENANT)
    assert len(rows) == 1
    assert rows[0].proposed_content == "draft v2"


async def test_update_suggestion_tool_on_resolved_raises_model_actionable_message() -> None:
    from epicurus_core import ToolError

    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    await module.call_tool("notes_create", {"slug": "n", "content": "x"})
    sid = (await sugg.list(tenant=TENANT))[0].sid
    await module.call_tool("notes_withdraw_suggestion", {"id": sid})
    with pytest.raises(ToolError, match=r"(?i)already withdrawn"):
        await module.call_tool("notes_update_suggestion", {"id": sid, "content": "too late"})


async def test_withdraw_suggestion_tool_removes_from_queue() -> None:
    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    await module.call_tool("notes_create", {"slug": "n", "content": "x"})
    sid = (await sugg.list(tenant=TENANT))[0].sid
    content, _ = await module.call_tool("notes_withdraw_suggestion", {"id": sid})
    assert "withdrawn" in content[0].text.lower()
    assert await sugg.list(tenant=TENANT) == []


async def test_withdraw_suggestion_tool_unknown_id_raises() -> None:
    from epicurus_core import ToolError

    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    with pytest.raises(ToolError, match=r"(?i)no such suggestion"):
        await module.call_tool("notes_withdraw_suggestion", {"id": "nope"})


async def test_suggestion_lifecycle_tools_are_not_side_effect_write() -> None:
    """list/read are 'read'; update/withdraw are 'propose' — never 'write' (#744, ADR-0112):
    both stay inside the pending queue and never touch a note."""
    store, sugg = await _stores()
    module, _ = await _module_for(store, sugg)
    manifest = await module.manifest()
    by_name = {t.name: t for t in manifest.tools}
    assert by_name["notes_list_suggestions"].side_effect == "read"
    assert by_name["notes_read_suggestion"].side_effect == "read"
    assert by_name["notes_update_suggestion"].side_effect == "propose"
    assert by_name["notes_withdraw_suggestion"].side_effect == "propose"
