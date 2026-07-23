"""Tests for the knowledge MCP tool surface — `knowledge_search` (indexers mocked)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import EpicurusModule, PlatformClient
from epicurus_core.contracts import ToolEnvelope
from epicurus_knowledge.indexer import KnowledgeIndexer, SearchHit
from epicurus_knowledge.refs import SOURCE_DOC, SOURCE_NOTE, decode_ref
from epicurus_knowledge.service import build_module
from epicurus_knowledge.suggestions import SuggestionReview, SuggestionStore


def _hit(note_path: str, text: str, score: float, heading: str | None = None) -> SearchHit:
    return SearchHit(note_path=note_path, heading=heading, text=text, score=score)


def _review_platform(store: SuggestionStore, vault: object, vault_path: Path):  # type: ignore[no-untyped-def]
    """A review + a platform mock (review on) for build_module in these tests.

    These tests exercise only the read-only navigation tools / the search surface / the
    manifest, never a vault write — so the mocked platform doubles as ``VaultPages``' file-API
    client (constructor requirement) without any write actually being made.
    """
    from epicurus_core import PlatformClient
    from epicurus_knowledge.pages import VaultPages
    from epicurus_knowledge.reader import DiskVaultReader
    from epicurus_knowledge.suggestions import SuggestionAuditStore, SuggestionReview

    platform = AsyncMock(spec=PlatformClient)
    platform.get_suggestions_enabled = AsyncMock(return_value=True)
    reader = DiskVaultReader(vault_path)
    pages = VaultPages(vault_path, vault, platform=platform, core_prefix="knowledge", reader=reader)  # type: ignore[arg-type]
    # Never exercised (no approve/reject here), so an uninitialised store is fine.
    audit = SuggestionAuditStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    review = SuggestionReview(store, pages, vault, reader=reader, tenant="test", audit=audit)  # type: ignore[arg-type]
    return review, platform, reader


def _module(vault_hits: list[SearchHit], docs_hits: list[SearchHit]) -> EpicurusModule:
    from epicurus_knowledge.module_docs import ModuleDocsIndexer

    vault = AsyncMock(spec=KnowledgeIndexer)
    vault.search = AsyncMock(return_value=vault_hits)
    docs = AsyncMock(spec=KnowledgeIndexer)
    docs.search = AsyncMock(return_value=docs_hits)
    module_docs = AsyncMock(spec=ModuleDocsIndexer)
    module_docs.run = AsyncMock(return_value={"indexed": 0, "deleted": 0, "unchanged": 0})
    # The search tests never reach the suggestion store; an uninitialised one is fine.
    suggestions = SuggestionStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    review, platform, reader = _review_platform(suggestions, vault, Path("/vault"))
    return build_module(
        vault,
        docs,
        module_docs,
        suggestions,
        review,
        platform,
        tenant="test",
        vault_path=Path("/vault"),
        reader=reader,
    )


def _envelope(content: list) -> ToolEnvelope:  # type: ignore[type-arg]
    return ToolEnvelope.model_validate_json(content[0].text)  # type: ignore[attr-defined]


async def test_search_returns_chunks_and_chips() -> None:
    module = _module(
        [_hit("note.md", "Vault answer about cats.", 0.9, "Cats")],
        [_hit("services/knowledge.md", "Docs answer.", 0.5)],
    )
    content, _ = await module.mcp.call_tool("knowledge_search", {"query": "cats"})
    env = _envelope(content)
    # The chunk text itself reaches the model (RAG content).
    assert "Vault answer about cats." in env.text
    assert "Docs answer." in env.text
    assert "docs/services/knowledge.md" in env.text  # platform-docs path is prefixed
    # One chip per cited document, highest score first.
    assert [r.kind for r in env.entity_refs] == ["knowledge", "knowledge"]
    assert decode_ref(env.entity_refs[0].ref_id) == (SOURCE_NOTE, "note.md")
    assert decode_ref(env.entity_refs[1].ref_id) == (SOURCE_DOC, "services/knowledge.md")
    assert env.entity_refs[0].title == "Cats"  # the matched heading labels the chip


async def test_search_dedupes_chips_per_document() -> None:
    module = _module(
        [_hit("note.md", "First chunk.", 0.9, "A"), _hit("note.md", "Second chunk.", 0.8, "B")],
        [],
    )
    content, _ = await module.mcp.call_tool("knowledge_search", {"query": "x"})
    env = _envelope(content)
    # Both chunks appear in the text, but the document yields a single chip.
    assert "First chunk." in env.text
    assert "Second chunk." in env.text
    assert len(env.entity_refs) == 1


async def test_search_empty_returns_no_chips() -> None:
    module = _module([], [])
    content, _ = await module.mcp.call_tool("knowledge_search", {"query": "nothing"})
    env = _envelope(content)
    assert env.entity_refs == []
    assert "No matching content" in env.text


async def test_manifest_declares_embedding_model_slot() -> None:
    """Knowledge declares an 'embedding' slot so the operator can pick the model (#128)."""
    manifest = await _module([], []).manifest()
    slots = {s.key: s for s in manifest.required_models}
    assert "embedding" in slots
    assert slots["embedding"].role == "embedding"
    assert slots["embedding"].label  # non-empty — shown on the Modules page


async def test_full_body_writes_open_the_document_pane() -> None:
    """The two tools whose `content` is the document's whole body carry the annotation (#541)."""
    tools = {t.name: t for t in (await _module([], []).manifest()).tools}

    for name in ("knowledge_create_document", "knowledge_propose_edit"):
        annotation = tools[name].writes_document
        assert annotation is not None, name
        assert annotation.content_arg == "content"
        assert annotation.target_arg == "path"
        # The body is the title's source (the module derives it), so there is no title arg.
        assert annotation.title_arg is None


async def test_structural_and_read_tools_do_not_open_the_document_pane() -> None:
    """Only a call carrying a document body should open a document pane — nothing else has one."""
    tools = {t.name: t for t in (await _module([], []).manifest()).tools}
    for name in (
        "knowledge_propose_move",
        "knowledge_propose_rename",
        "knowledge_propose_folder",
        "knowledge_propose_project",
        "knowledge_search",
    ):
        assert tools[name].writes_document is None, name


# ── navigation tools over a real vault (#KB-refactor) ─────────────────────────


def _nav_module(vault_path: Path) -> EpicurusModule:
    """A module whose vault is a real directory, for the read-only navigation tools."""
    from epicurus_knowledge.module_docs import ModuleDocsIndexer

    vault = AsyncMock(spec=KnowledgeIndexer)
    docs = AsyncMock(spec=KnowledgeIndexer)
    module_docs = AsyncMock(spec=ModuleDocsIndexer)
    suggestions = SuggestionStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    review, platform, reader = _review_platform(suggestions, vault, vault_path)
    return build_module(
        vault,
        docs,
        module_docs,
        suggestions,
        review,
        platform,
        tenant="test",
        vault_path=vault_path,
        reader=reader,
    )


def _text(content: list) -> str:  # type: ignore[type-arg]
    # The navigation tools return plain text, not a ToolEnvelope.
    return content[0].text  # type: ignore[attr-defined,no-any-return]


async def test_list_projects_lists_top_level_folders(tmp_path: Path) -> None:
    (tmp_path / "personal").mkdir()
    (tmp_path / "work").mkdir()
    (tmp_path / "_reserved").mkdir()  # underscore-prefixed: never a project
    module = _nav_module(tmp_path)
    content, _ = await module.mcp.call_tool("knowledge_list_projects", {})
    out = _text(content)
    assert "personal" in out and "work" in out
    assert "_reserved" not in out


async def test_list_projects_empty(tmp_path: Path) -> None:
    module = _nav_module(tmp_path)
    content, _ = await module.mcp.call_tool("knowledge_list_projects", {})
    assert "No knowledge bases" in _text(content)


async def test_tree_shows_structure(tmp_path: Path) -> None:
    (tmp_path / "kb" / "sub").mkdir(parents=True)
    (tmp_path / "kb" / "alpha.md").write_text("# A\n", encoding="utf-8")
    (tmp_path / "kb" / "sub" / "beta.md").write_text("# B\n", encoding="utf-8")
    module = _nav_module(tmp_path)
    content, _ = await module.mcp.call_tool("knowledge_tree", {"project": "kb"})
    out = _text(content)
    assert "kb/" in out
    assert "sub/" in out
    assert "alpha.md" in out
    assert "beta.md" in out


async def test_read_document_returns_content(tmp_path: Path) -> None:
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "a.md").write_text("# Hello\nbody\n", encoding="utf-8")
    module = _nav_module(tmp_path)
    content, _ = await module.mcp.call_tool("knowledge_read_document", {"path": "kb/a.md"})
    assert "# Hello" in _text(content)


async def test_read_document_missing(tmp_path: Path) -> None:
    (tmp_path / "kb").mkdir()
    module = _nav_module(tmp_path)
    content, _ = await module.mcp.call_tool("knowledge_read_document", {"path": "kb/missing.md"})
    assert "No such document" in _text(content)


async def test_read_document_rejects_traversal(tmp_path: Path) -> None:
    module = _nav_module(tmp_path)
    content, _ = await module.mcp.call_tool("knowledge_read_document", {"path": "../escape.md"})
    assert "cannot read" in _text(content).lower()


# ── rejected writes raise, not a success envelope (#690) ─────────────────────
#
# `writes_document`-annotated tools ride the live document pane: the pane keys `doc.failed`
# off the MCP call's structural `isError`, not the returned text (core-app's `_invoke`
# docstring). Before #690 these guard clauses returned a normal `tool_envelope`, so a
# rejected write left `is_error=False` and the pane opened an editor on content that was
# never written. `pytest.raises(ToolError)` is how FastMCP surfaces a raised exception
# from inside a `@module.tool()` function (proven pattern: calendar's
# `test_calendar_update_event_tool_unknown_raises`).


async def test_create_document_rejects_bad_path_by_raising(tmp_path: Path) -> None:
    """`knowledge_propose_edit`'s equivalent rejections are covered in test_suggestions.py
    (bad operation / traversal / non-.md / existing path / structural operation) — this one
    exercises `knowledge_create_document`, the other `writes_document`-annotated caller of
    the shared `_stage_doc_write`."""
    module = _nav_module(tmp_path)
    with pytest.raises(ToolError, match="Cannot propose change"):
        await module.mcp.call_tool(
            "knowledge_create_document", {"path": "notes.txt", "content": "x"}
        )


async def test_finalize_apply_failure_raises_not_a_success_envelope(tmp_path: Path) -> None:
    """Review off + a failed direct-apply must fail the call. The suggestion stays staged
    either way (`_finalize`'s docstring) — but the pane must not treat `doc.target` as
    written when the apply it asked for did not happen."""
    from epicurus_knowledge.module_docs import ModuleDocsIndexer
    from epicurus_knowledge.reader import DiskVaultReader

    vault = AsyncMock(spec=KnowledgeIndexer)
    docs = AsyncMock(spec=KnowledgeIndexer)
    module_docs = AsyncMock(spec=ModuleDocsIndexer)
    suggestions = SuggestionStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await suggestions.init()
    reader = DiskVaultReader(tmp_path)
    platform = AsyncMock(spec=PlatformClient)
    platform.get_suggestions_enabled = AsyncMock(return_value=False)  # review is off
    review = AsyncMock(spec=SuggestionReview)
    review.approve = AsyncMock(side_effect=RuntimeError("disk full"))
    module = build_module(
        vault,
        docs,
        module_docs,
        suggestions,
        review,
        platform,
        tenant="test",
        vault_path=tmp_path,
        reader=reader,
    )
    with pytest.raises(ToolError, match=r"applying failed.*disk full"):
        await module.mcp.call_tool(
            "knowledge_create_document", {"path": "kb/new.md", "content": "hello"}
        )
