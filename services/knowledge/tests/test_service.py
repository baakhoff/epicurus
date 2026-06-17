"""Tests for the knowledge MCP tool surface — `knowledge_search` (indexers mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from epicurus_core import EpicurusModule
from epicurus_core.contracts import ToolEnvelope
from epicurus_knowledge.indexer import KnowledgeIndexer, SearchHit
from epicurus_knowledge.refs import SOURCE_DOC, SOURCE_NOTE, decode_ref
from epicurus_knowledge.service import build_module


def _hit(note_path: str, text: str, score: float, heading: str | None = None) -> SearchHit:
    return SearchHit(note_path=note_path, heading=heading, text=text, score=score)


def _module(vault_hits: list[SearchHit], docs_hits: list[SearchHit]) -> EpicurusModule:
    vault = AsyncMock(spec=KnowledgeIndexer)
    vault.search = AsyncMock(return_value=vault_hits)
    docs = AsyncMock(spec=KnowledgeIndexer)
    docs.search = AsyncMock(return_value=docs_hits)
    return build_module(vault, docs)


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
