"""Unit tests for the incremental vault indexer.

Uses SQLite in-memory for the NoteIndex and a lightweight fake for the Qdrant
client and PlatformClient, so no Docker infra is needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_knowledge.db import NoteIndex
from epicurus_knowledge.indexer import KnowledgeIndexer

TENANT = "test"
EMBED_DIM = 4


def _fake_vectors(texts: list[str]) -> list[list[float]]:
    return [[float(i), 0.0, 0.0, 0.0] for i in range(len(texts))]


def _make_mock_platform() -> Any:
    platform = MagicMock()
    platform.embed = AsyncMock(side_effect=lambda texts, **_: _fake_vectors(texts))
    return platform


def _make_mock_qdrant() -> Any:
    qdrant = MagicMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    qdrant.create_collection = AsyncMock()
    qdrant.upsert = AsyncMock()
    qdrant.delete = AsyncMock()
    return qdrant


@pytest.fixture
async def note_index() -> NoteIndex:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx = NoteIndex(engine)
    await idx.init()
    return idx


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A minimal vault with two notes."""
    (tmp_path / "note_a.md").write_text("# Note A\n\nContent of A.")
    (tmp_path / "note_b.md").write_text("# Note B\n\nContent of B.")
    return tmp_path


def _make_indexer(note_index: NoteIndex, vault: Path) -> KnowledgeIndexer:
    return KnowledgeIndexer(
        note_index,
        _make_mock_qdrant(),
        _make_mock_platform(),
        vault_path=vault,
        tenant=TENANT,
    )


async def test_first_run_indexes_all_notes(note_index: NoteIndex, vault: Path) -> None:
    indexer = _make_indexer(note_index, vault)
    result = await indexer.run()
    assert result["indexed"] == 2
    assert result["deleted"] == 0
    assert result["unchanged"] == 0


async def test_second_run_skips_unchanged_notes(note_index: NoteIndex, vault: Path) -> None:
    indexer = _make_indexer(note_index, vault)
    await indexer.run()
    result = await indexer.run()
    assert result["unchanged"] == 2
    assert result["indexed"] == 0


async def test_modified_note_is_reindexed(note_index: NoteIndex, vault: Path) -> None:
    indexer = _make_indexer(note_index, vault)
    await indexer.run()
    # Overwrite note_a with different content to change its hash.
    note_a = vault / "note_a.md"
    note_a.write_text("# Note A Updated\n\nNew content.")
    # Force mtime change by explicitly changing mtime_ns in the DB to differ from actual.
    # Busy-loop is flaky; instead directly mutate the record in the DB.
    rec = await note_index.get(tenant=TENANT, note_path="note_a.md")
    assert rec is not None
    await note_index.upsert(
        tenant=TENANT,
        note_path="note_a.md",
        mtime_ns=0,  # guaranteed to differ from real mtime
        content_hash=rec.content_hash,
        chunk_count=rec.chunk_count,
    )
    result = await indexer.run()
    assert result["indexed"] >= 1


async def test_deleted_note_is_removed(note_index: NoteIndex, vault: Path) -> None:
    indexer = _make_indexer(note_index, vault)
    await indexer.run()
    (vault / "note_b.md").unlink()
    result = await indexer.run()
    assert result["deleted"] == 1
    # The deleted note should no longer appear in the index.
    remaining = await note_index.list_paths(tenant=TENANT)
    assert "note_b.md" not in remaining


async def test_non_markdown_files_are_ignored(note_index: NoteIndex, vault: Path) -> None:
    (vault / "image.png").write_bytes(b"\x89PNG")
    (vault / "config.yaml").write_text("key: value")
    indexer = _make_indexer(note_index, vault)
    result = await indexer.run()
    # Only the two .md files should be indexed.
    assert result["indexed"] == 2


async def test_missing_vault_returns_zeros(note_index: NoteIndex, tmp_path: Path) -> None:
    missing = tmp_path / "no_such_vault"
    indexer = KnowledgeIndexer(
        note_index,
        _make_mock_qdrant(),
        _make_mock_platform(),
        vault_path=missing,
        tenant=TENANT,
    )
    result = await indexer.run()
    assert result == {"indexed": 0, "deleted": 0, "unchanged": 0}


async def test_tenant_isolation(tmp_path: Path) -> None:
    """Notes indexed for tenant-a must not appear under tenant-b."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx_a = NoteIndex(engine)
    await idx_a.init()

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "shared.md").write_text("# Shared\n\ntext")

    indexer_a = KnowledgeIndexer(
        idx_a, _make_mock_qdrant(), _make_mock_platform(), vault_path=vault, tenant="tenant-a"
    )
    await indexer_a.run()

    paths_a = await idx_a.list_paths(tenant="tenant-a")
    paths_b = await idx_a.list_paths(tenant="tenant-b")
    assert "shared.md" in paths_a
    assert "shared.md" not in paths_b


async def test_mcp_tool_reindex(note_index: NoteIndex, vault: Path) -> None:
    from epicurus_knowledge.service import build_module

    indexer = _make_indexer(note_index, vault)
    module = build_module(indexer)
    _content, structured = await module.mcp.call_tool("knowledge_reindex", {})
    assert isinstance(structured, dict)
    payload: dict[str, object] = structured.get("result") or structured  # type: ignore[assignment]
    assert "indexed" in payload


async def test_manifest_declares_tool_and_event(note_index: NoteIndex, vault: Path) -> None:
    from epicurus_knowledge.service import build_module

    indexer = _make_indexer(note_index, vault)
    module = build_module(indexer)
    manifest = await module.manifest()
    tool_names = {t.name for t in manifest.tools}
    assert "knowledge_reindex" in tool_names
    assert "knowledge_search" in tool_names
    assert any(e.subject == "knowledge.index.completed" for e in manifest.events_emitted)
    assert manifest.ui is not None
    assert manifest.ui.status_url == "/status"


async def test_search_empty_when_no_collection(note_index: NoteIndex, vault: Path) -> None:
    qdrant = _make_mock_qdrant()
    qdrant.collection_exists = AsyncMock(return_value=False)
    indexer = KnowledgeIndexer(
        note_index,
        qdrant,
        _make_mock_platform(),
        vault_path=vault,
        tenant=TENANT,
    )
    results = await indexer.search("anything")
    assert results == []


async def test_search_returns_hits(note_index: NoteIndex, vault: Path) -> None:
    from unittest.mock import MagicMock

    qdrant = _make_mock_qdrant()

    # Fake Qdrant ScoredPoint result.
    hit = MagicMock()
    hit.score = 0.9
    hit.payload = {
        "note_path": "note_a.md",
        "heading": "Note A",
        "text": "Content of A.",
    }
    qdrant.search = AsyncMock(return_value=[hit])

    indexer = KnowledgeIndexer(
        note_index,
        qdrant,
        _make_mock_platform(),
        vault_path=vault,
        tenant=TENANT,
    )
    results = await indexer.search("content of A", k=1)
    assert len(results) == 1
    assert results[0]["note_path"] == "note_a.md"
    assert results[0]["heading"] == "Note A"
    assert results[0]["text"] == "Content of A."
    assert results[0]["score"] == pytest.approx(0.9)


async def test_search_skips_hits_with_no_payload(note_index: NoteIndex, vault: Path) -> None:
    from unittest.mock import MagicMock

    qdrant = _make_mock_qdrant()
    hit_no_payload = MagicMock()
    hit_no_payload.score = 0.5
    hit_no_payload.payload = None
    qdrant.search = AsyncMock(return_value=[hit_no_payload])

    indexer = KnowledgeIndexer(
        note_index,
        qdrant,
        _make_mock_platform(),
        vault_path=vault,
        tenant=TENANT,
    )
    results = await indexer.search("query")
    assert results == []


async def test_mcp_tool_search(note_index: NoteIndex, vault: Path) -> None:
    from unittest.mock import MagicMock

    from epicurus_knowledge.service import build_module

    qdrant = _make_mock_qdrant()
    hit = MagicMock()
    hit.score = 0.8
    hit.payload = {"note_path": "note_b.md", "heading": None, "text": "Content of B."}
    qdrant.search = AsyncMock(return_value=[hit])

    indexer = KnowledgeIndexer(
        note_index,
        qdrant,
        _make_mock_platform(),
        vault_path=vault,
        tenant=TENANT,
    )
    module = build_module(indexer)
    _content, structured = await module.mcp.call_tool("knowledge_search", {"query": "B", "k": 1})
    assert structured is not None
