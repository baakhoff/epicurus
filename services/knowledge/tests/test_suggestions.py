"""Tests for the suggested-changes review queue (ADR-0033, #220).

Covers:
- ``SuggestionStore`` CRUD + tenant scoping + ordering
- ``validate_operation`` normalisation
- ``SuggestionReview`` — diff computation, approve (create/update/delete), reject, 404s
- the ``review`` page HTTP surface + the literal-vs-param route ordering vs the editor
- the ``knowledge_propose_edit`` MCP tool — stages, never writes; rejects bad input
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core.contracts import ToolEnvelope
from epicurus_knowledge.indexer import KnowledgeIndexer
from epicurus_knowledge.pages import VaultPages, create_pages_router
from epicurus_knowledge.service import build_module
from epicurus_knowledge.suggestions import (
    SuggestionReview,
    SuggestionStore,
    create_review_router,
    validate_operation,
)

TENANT = "test"


# ── fixtures ──────────────────────────────────────────────────────────────────


class _FakeIndexer:
    """Records index/de-index calls so we can assert approve wiring without Qdrant."""

    def __init__(self) -> None:
        self.indexed: list[str] = []
        self.removed: list[str] = []
        self.ran = 0

    async def index_path(self, rel: str) -> int:
        self.indexed.append(rel)
        return 1

    async def remove_path(self, rel: str) -> None:
        self.removed.append(rel)

    async def run(self) -> dict[str, int]:
        # A folder move reconciles via a full incremental pass (#KB-refactor).
        self.ran += 1
        return {"indexed": 0, "deleted": 0, "unchanged": 0}


async def _store() -> SuggestionStore:
    store = SuggestionStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.init()
    return store


async def _add(
    store: SuggestionStore,
    *,
    path: str,
    operation: str,
    content: str = "",
    origin: str = "agent",
    note: str = "",
    tenant: str = TENANT,
    to_path: str = "",
) -> str:
    s = await store.add(
        tenant=tenant,
        path=path,
        operation=operation,
        proposed_content=content,
        origin=origin,
        note=note,
        to_path=to_path,
    )
    return s.sid


# ── SuggestionStore ───────────────────────────────────────────────────────────


async def test_store_add_returns_suggestion_with_sid() -> None:
    store = await _store()
    s = await store.add(
        tenant=TENANT,
        path="a.md",
        operation="create",
        proposed_content="# A\n",
        origin="agent",
        note="why",
    )
    assert s.sid and len(s.sid) == 32  # uuid4 hex
    assert s.path == "a.md"
    assert s.operation == "create"
    assert s.proposed_content == "# A\n"
    assert s.note == "why"
    assert s.created_at is not None


async def test_store_list_is_ordered_oldest_first() -> None:
    store = await _store()
    await _add(store, path="first.md", operation="create")
    await _add(store, path="second.md", operation="create")
    rows = await store.list(tenant=TENANT)
    assert [r.path for r in rows] == ["first.md", "second.md"]


async def test_store_get_and_delete() -> None:
    store = await _store()
    sid = await _add(store, path="a.md", operation="create")
    assert (await store.get(tenant=TENANT, sid=sid)) is not None
    assert await store.delete(tenant=TENANT, sid=sid) is True
    assert (await store.get(tenant=TENANT, sid=sid)) is None
    # Deleting an unknown id is a no-op returning False.
    assert await store.delete(tenant=TENANT, sid="deadbeef") is False


async def test_store_is_tenant_scoped() -> None:
    store = await _store()
    await _add(store, path="a.md", operation="create", tenant="tenant-a")
    assert len(await store.list(tenant="tenant-a")) == 1
    assert await store.list(tenant="tenant-b") == []


# ── validate_operation ────────────────────────────────────────────────────────


@pytest.mark.parametrize("op", ["create", "update", "delete", "CREATE", " Update "])
def test_validate_operation_accepts_known(op: str) -> None:
    assert validate_operation(op) in {"create", "update", "delete"}


@pytest.mark.parametrize("op", ["", "remove", "patch", "rename"])
def test_validate_operation_rejects_unknown(op: str) -> None:
    with pytest.raises(ValueError):
        validate_operation(op)


# ── SuggestionReview: diff + apply ────────────────────────────────────────────


async def _review(tmp_path: Path) -> tuple[SuggestionReview, SuggestionStore, _FakeIndexer]:
    store = await _store()
    indexer = _FakeIndexer()
    pages = VaultPages(tmp_path, indexer)  # type: ignore[arg-type]
    review = SuggestionReview(store, pages, indexer, vault_path=tmp_path, tenant=TENANT)  # type: ignore[arg-type]
    return review, store, indexer


async def test_review_create_diff_is_all_additions(tmp_path: Path) -> None:
    review, store, _ = await _review(tmp_path)
    await _add(store, path="new.md", operation="create", content="# New\nbody\n")
    data = await review.list_review()
    assert len(data.suggestions) == 1
    diff = data.suggestions[0].diff
    assert "+# New" in diff
    assert "+body" in diff


async def test_review_update_diff_shows_delta(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("line1\nline2\n", encoding="utf-8")
    review, store, _ = await _review(tmp_path)
    await _add(store, path="doc.md", operation="update", content="line1\nCHANGED\n")
    diff = (await review.list_review()).suggestions[0].diff
    assert "-line2" in diff
    assert "+CHANGED" in diff


async def test_review_delete_diff_is_all_removals(tmp_path: Path) -> None:
    (tmp_path / "doomed.md").write_text("gone\n", encoding="utf-8")
    review, store, _ = await _review(tmp_path)
    await _add(store, path="doomed.md", operation="delete")
    diff = (await review.list_review()).suggestions[0].diff
    assert "-gone" in diff


async def test_approve_create_writes_and_indexes_then_drops(tmp_path: Path) -> None:
    review, store, indexer = await _review(tmp_path)
    sid = await _add(store, path="note.md", operation="create", content="# Note\n")
    result = await review.approve(sid)
    assert result.status == "approved"
    assert (tmp_path / "note.md").read_text(encoding="utf-8") == "# Note\n"
    assert "note.md" in indexer.indexed
    assert await store.list(tenant=TENANT) == []  # dropped from the queue


async def test_approve_update_overwrites_existing(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("old\n", encoding="utf-8")
    review, store, indexer = await _review(tmp_path)
    sid = await _add(store, path="doc.md", operation="update", content="new\n")
    await review.approve(sid)
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "new\n"
    assert "doc.md" in indexer.indexed


async def test_approve_delete_unlinks_and_deindexes(tmp_path: Path) -> None:
    (tmp_path / "doomed.md").write_text("bye\n", encoding="utf-8")
    review, store, indexer = await _review(tmp_path)
    sid = await _add(store, path="doomed.md", operation="delete")
    result = await review.approve(sid)
    assert result.status == "approved"
    assert not (tmp_path / "doomed.md").exists()
    assert "doomed.md" in indexer.removed
    assert await store.list(tenant=TENANT) == []


async def test_reject_discards_without_touching_vault(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("original\n", encoding="utf-8")
    review, store, indexer = await _review(tmp_path)
    sid = await _add(store, path="doc.md", operation="update", content="tampered\n")
    result = await review.reject(sid)
    assert result.status == "rejected"
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "original\n"  # untouched
    assert indexer.indexed == [] and indexer.removed == []
    assert await store.list(tenant=TENANT) == []


# ── read-only mode (watched external vault, #232) ─────────────────────────────


async def _read_only_review(
    tmp_path: Path,
) -> tuple[SuggestionReview, SuggestionStore, _FakeIndexer]:
    store = await _store()
    indexer = _FakeIndexer()
    pages = VaultPages(tmp_path, indexer, read_only=True)  # type: ignore[arg-type]
    review = SuggestionReview(
        store,
        pages,
        indexer,
        vault_path=tmp_path,
        tenant=TENANT,
        read_only=True,  # type: ignore[arg-type]
    )
    return review, store, indexer


async def test_approve_is_409_when_read_only_and_keeps_the_suggestion(tmp_path: Path) -> None:
    from fastapi import HTTPException

    review, store, indexer = await _read_only_review(tmp_path)
    sid = await _add(store, path="note.md", operation="create", content="# Note\n")
    with pytest.raises(HTTPException) as err:
        await review.approve(sid)
    assert err.value.status_code == 409
    # Nothing written or indexed, and the suggestion stays queued for when writing resumes.
    assert not (tmp_path / "note.md").exists()
    assert indexer.indexed == []
    assert len(await store.list(tenant=TENANT)) == 1


async def test_reject_still_works_when_read_only(tmp_path: Path) -> None:
    # Read-only blocks the *apply*, not queue hygiene — the operator can still clear it.
    review, store, _ = await _read_only_review(tmp_path)
    sid = await _add(store, path="note.md", operation="create", content="# Note\n")
    result = await review.reject(sid)
    assert result.status == "rejected"
    assert await store.list(tenant=TENANT) == []


async def test_approve_unknown_suggestion_404(tmp_path: Path) -> None:
    review, _, _ = await _review(tmp_path)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        await review.approve("nope")
    assert err.value.status_code == 404


async def test_reject_unknown_suggestion_404(tmp_path: Path) -> None:
    review, _, _ = await _review(tmp_path)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        await review.reject("nope")
    assert err.value.status_code == 404


# ── HTTP surface + route ordering ─────────────────────────────────────────────


def _app(review: SuggestionReview, pages: VaultPages) -> TestClient:
    """Mount review BEFORE pages, mirroring app.py, so /pages/review wins the match."""
    app = FastAPI()
    app.include_router(create_review_router(review))
    app.include_router(create_pages_router(pages))
    return TestClient(app, raise_server_exceptions=True)


async def test_review_endpoint_lists_pending(tmp_path: Path) -> None:
    review, store, indexer = await _review(tmp_path)
    await _add(store, path="a.md", operation="create", content="# A\n")
    pages = VaultPages(tmp_path, indexer)  # type: ignore[arg-type]
    client = _app(review, pages)
    resp = client.get("/pages/review")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["suggestions"]) == 1
    assert body["suggestions"][0]["path"] == "a.md"


async def test_review_route_does_not_shadow_editor_vault(tmp_path: Path) -> None:
    """GET /pages/review hits review; GET /pages/vault still hits the editor (#216)."""
    (tmp_path / "x.md").write_text("# X\n", encoding="utf-8")
    review, _, indexer = await _review(tmp_path)
    pages = VaultPages(tmp_path, indexer)  # type: ignore[arg-type]
    client = _app(review, pages)
    assert client.get("/pages/review").status_code == 200
    vault_resp = client.get("/pages/vault")
    assert vault_resp.status_code == 200
    assert "docs" in vault_resp.json()  # EditorData, not ReviewData


async def test_approve_endpoint_applies(tmp_path: Path) -> None:
    review, store, indexer = await _review(tmp_path)
    sid = await _add(store, path="note.md", operation="create", content="# Note\n")
    pages = VaultPages(tmp_path, indexer)  # type: ignore[arg-type]
    client = _app(review, pages)
    resp = client.post(f"/pages/review/suggestions/{sid}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert (tmp_path / "note.md").exists()


async def test_reject_endpoint_discards(tmp_path: Path) -> None:
    review, store, indexer = await _review(tmp_path)
    sid = await _add(store, path="note.md", operation="create", content="# Note\n")
    pages = VaultPages(tmp_path, indexer)  # type: ignore[arg-type]
    client = _app(review, pages)
    resp = client.post(f"/pages/review/suggestions/{sid}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert not (tmp_path / "note.md").exists()


# ── the knowledge_propose_edit tool ───────────────────────────────────────────


def _module_with_store(store: SuggestionStore, vault_path: Path, *, review_on: bool = True):  # type: ignore[no-untyped-def]
    from epicurus_core import PlatformClient
    from epicurus_knowledge.module_docs import ModuleDocsIndexer

    vault = AsyncMock(spec=KnowledgeIndexer)
    vault.index_path = AsyncMock(return_value=1)
    vault.remove_path = AsyncMock(return_value=None)
    docs = AsyncMock(spec=KnowledgeIndexer)
    module_docs = AsyncMock(spec=ModuleDocsIndexer)
    pages = VaultPages(vault_path, vault)  # type: ignore[arg-type]
    review = SuggestionReview(store, pages, vault, vault_path=vault_path, tenant=TENANT)  # type: ignore[arg-type]
    platform = AsyncMock(spec=PlatformClient)
    platform.get_suggestions_enabled = AsyncMock(return_value=review_on)
    return build_module(
        vault, docs, module_docs, store, review, platform, tenant=TENANT, vault_path=vault_path
    )


def _envelope(content: list) -> ToolEnvelope:  # type: ignore[type-arg]
    return ToolEnvelope.model_validate_json(content[0].text)  # type: ignore[attr-defined]


async def test_propose_edit_stages_a_suggestion(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool(
        "knowledge_propose_edit",
        {"path": "ideas/new.md", "content": "# Idea\n", "operation": "create"},
    )
    env = _envelope(content)
    assert "pending your review" in env.text.lower()
    rows = await store.list(tenant=TENANT)
    assert len(rows) == 1
    assert rows[0].path == "ideas/new.md"
    assert rows[0].operation == "create"
    assert rows[0].origin == "agent"


async def test_propose_edit_rejects_bad_operation(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool(
        "knowledge_propose_edit",
        {"path": "a.md", "content": "x", "operation": "rename"},
    )
    env = _envelope(content)
    assert "operation must be one of" in env.text
    assert await store.list(tenant=TENANT) == []  # nothing staged


async def test_propose_edit_rejects_traversal_path(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool(
        "knowledge_propose_edit",
        {"path": "../escape.md", "content": "x", "operation": "create"},
    )
    env = _envelope(content)
    assert "cannot propose change" in env.text.lower()
    assert await store.list(tenant=TENANT) == []


async def test_propose_edit_rejects_non_md_path(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool(
        "knowledge_propose_edit",
        {"path": "notes.txt", "content": "x", "operation": "create"},
    )
    env = _envelope(content)
    assert "cannot propose change" in env.text.lower()
    assert await store.list(tenant=TENANT) == []


# ── the dedicated knowledge_create_document tool ──────────────────────────────


async def test_create_document_stages_a_create_suggestion(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool(
        "knowledge_create_document",
        {"path": "ideas/new.md", "content": "# Idea\n"},
    )
    env = _envelope(content)
    assert "pending your review" in env.text.lower()
    rows = await store.list(tenant=TENANT)
    assert len(rows) == 1
    assert rows[0].path == "ideas/new.md"
    assert rows[0].operation == "create"
    assert rows[0].origin == "agent"


async def test_create_document_rejects_an_existing_path(tmp_path: Path) -> None:
    (tmp_path / "existing.md").write_text("# Already here\n", encoding="utf-8")
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool(
        "knowledge_create_document",
        {"path": "existing.md", "content": "# New\n"},
    )
    env = _envelope(content)
    assert "already exists" in env.text.lower()
    assert await store.list(tenant=TENANT) == []  # nothing staged


async def test_create_document_applies_directly_when_review_off(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path, review_on=False)
    content, _ = await module.mcp.call_tool(
        "knowledge_create_document",
        {"path": "kb/auto.md", "content": "# Auto\n"},
    )
    env = _envelope(content)
    assert "applied directly" in env.text.lower()


async def test_manifest_declares_review_page(tmp_path: Path) -> None:
    store = await _store()
    manifest = await _module_with_store(store, tmp_path).manifest()
    review_pages = [p for p in manifest.pages if p.archetype == "review"]
    assert len(review_pages) == 1
    assert review_pages[0].id == "review"


# ── new operations: move / mkdir / mkproject + content override (#KB-refactor) ─


@pytest.mark.parametrize("op", ["move", "mkdir", "mkproject"])
def test_validate_operation_accepts_structural(op: str) -> None:
    assert validate_operation(op) == op


async def test_store_roundtrips_to_path() -> None:
    store = await _store()
    s = await store.add(
        tenant=TENANT,
        path="a.md",
        operation="move",
        proposed_content="",
        origin="agent",
        note="",
        to_path="b.md",
    )
    got = await store.get(tenant=TENANT, sid=s.sid)
    assert got is not None and got.to_path == "b.md"


async def test_review_move_has_empty_diff_and_to_path(tmp_path: Path) -> None:
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "a.md").write_text("# A\n", encoding="utf-8")
    review, store, _ = await _review(tmp_path)
    await _add(store, path="kb/a.md", operation="move", to_path="kb/b.md")
    s = (await review.list_review()).suggestions[0]
    assert s.operation == "move"
    assert s.to_path == "kb/b.md"
    assert s.diff == ""  # structural ops carry no content diff


async def test_approve_move_file_relocates_and_reindexes(tmp_path: Path) -> None:
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "a.md").write_text("# A\n", encoding="utf-8")
    review, store, indexer = await _review(tmp_path)
    sid = await _add(store, path="kb/a.md", operation="move", to_path="kb/b.md")
    result = await review.approve(sid)
    assert result.status == "approved"
    assert result.path == "kb/b.md"
    assert not (tmp_path / "kb" / "a.md").exists()
    assert (tmp_path / "kb" / "b.md").is_file()
    assert "kb/a.md" in indexer.removed
    assert "kb/b.md" in indexer.indexed
    assert await store.list(tenant=TENANT) == []


async def test_approve_move_folder_reconciles_index(tmp_path: Path) -> None:
    (tmp_path / "kb" / "old").mkdir(parents=True)
    (tmp_path / "kb" / "old" / "c.md").write_text("# C\n", encoding="utf-8")
    review, store, indexer = await _review(tmp_path)
    sid = await _add(store, path="kb/old", operation="move", to_path="kb/new")
    await review.approve(sid)
    assert (tmp_path / "kb" / "new" / "c.md").is_file()
    assert not (tmp_path / "kb" / "old").exists()
    assert indexer.ran == 1  # a folder move triggers a full incremental reconcile


async def test_approve_mkdir_creates_folder(tmp_path: Path) -> None:
    (tmp_path / "kb").mkdir()
    review, store, _ = await _review(tmp_path)
    sid = await _add(store, path="kb/ideas", operation="mkdir")
    await review.approve(sid)
    assert (tmp_path / "kb" / "ideas").is_dir()
    assert await store.list(tenant=TENANT) == []


async def test_approve_mkproject_creates_top_level_folder(tmp_path: Path) -> None:
    review, store, _ = await _review(tmp_path)
    sid = await _add(store, path="research", operation="mkproject")
    result = await review.approve(sid)
    assert result.status == "approved"
    assert (tmp_path / "research").is_dir()
    assert await store.list(tenant=TENANT) == []


async def test_approve_update_honours_content_override(tmp_path: Path) -> None:
    # Per-hunk review (#KB-refactor): the operator approves a merged result, not the
    # agent's full proposal.
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "doc.md").write_text("old\n", encoding="utf-8")
    review, store, _ = await _review(tmp_path)
    sid = await _add(store, path="kb/doc.md", operation="update", content="full proposal\n")
    await review.approve(sid, content="operator merged\n")
    assert (tmp_path / "kb" / "doc.md").read_text(encoding="utf-8") == "operator merged\n"


async def test_approve_endpoint_accepts_content_body(tmp_path: Path) -> None:
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "doc.md").write_text("old\n", encoding="utf-8")
    review, store, indexer = await _review(tmp_path)
    sid = await _add(store, path="kb/doc.md", operation="update", content="proposal\n")
    pages = VaultPages(tmp_path, indexer)  # type: ignore[arg-type]
    client = _app(review, pages)
    resp = client.post(f"/pages/review/suggestions/{sid}/approve", json={"content": "merged\n"})
    assert resp.status_code == 200
    assert (tmp_path / "kb" / "doc.md").read_text(encoding="utf-8") == "merged\n"


# ── the structural propose tools ──────────────────────────────────────────────


async def test_propose_move_stages_suggestion(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool(
        "knowledge_propose_move", {"from_path": "kb/a.md", "to_path": "kb/sub/a.md"}
    )
    env = _envelope(content)
    assert "pending your review" in env.text.lower()
    rows = await store.list(tenant=TENANT)
    assert len(rows) == 1
    assert rows[0].operation == "move"
    assert rows[0].path == "kb/a.md"
    assert rows[0].to_path == "kb/sub/a.md"


async def test_propose_folder_stages_suggestion(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool("knowledge_propose_folder", {"path": "kb/ideas"})
    _envelope(content)
    rows = await store.list(tenant=TENANT)
    assert len(rows) == 1 and rows[0].operation == "mkdir" and rows[0].path == "kb/ideas"


async def test_propose_project_stages_suggestion(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool("knowledge_propose_project", {"name": "research"})
    _envelope(content)
    rows = await store.list(tenant=TENANT)
    assert len(rows) == 1 and rows[0].operation == "mkproject" and rows[0].path == "research"


async def test_propose_project_rejects_bad_name(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool("knowledge_propose_project", {"name": "a/b"})
    env = _envelope(content)
    assert "cannot propose" in env.text.lower()
    assert await store.list(tenant=TENANT) == []


async def test_propose_edit_rejects_structural_operation(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool(
        "knowledge_propose_edit", {"path": "kb/a.md", "content": "x", "operation": "move"}
    )
    env = _envelope(content)
    assert "structural" in env.text.lower()
    assert await store.list(tenant=TENANT) == []


async def test_propose_rename_stages_a_move(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool(
        "knowledge_propose_rename", {"path": "kb/a.md", "new_name": "b"}
    )
    env = _envelope(content)
    assert "pending your review" in env.text.lower()
    rows = await store.list(tenant=TENANT)
    assert len(rows) == 1
    assert rows[0].operation == "move"
    assert rows[0].path == "kb/a.md"
    assert rows[0].to_path == "kb/b.md"  # same folder, .md suffix preserved


async def test_propose_rename_rejects_a_slash(tmp_path: Path) -> None:
    store = await _store()
    module = _module_with_store(store, tmp_path)
    content, _ = await module.mcp.call_tool(
        "knowledge_propose_rename", {"path": "kb/a.md", "new_name": "sub/b"}
    )
    env = _envelope(content)
    assert "bare name" in env.text.lower()
    assert await store.list(tenant=TENANT) == []


async def test_propose_edit_auto_applies_when_review_off(tmp_path: Path) -> None:
    # With review turned off, the agent's change is applied directly — nothing left pending.
    store = await _store()
    module = _module_with_store(store, tmp_path, review_on=False)
    content, _ = await module.mcp.call_tool(
        "knowledge_propose_edit",
        {"path": "kb/new.md", "content": "# Auto\n", "operation": "create"},
    )
    env = _envelope(content)
    assert "applied directly" in env.text.lower()
    assert (tmp_path / "kb" / "new.md").read_text(encoding="utf-8") == "# Auto\n"
    assert await store.list(tenant=TENANT) == []
