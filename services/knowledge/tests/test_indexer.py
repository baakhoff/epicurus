"""Unit tests for the incremental vault indexer.

Uses SQLite in-memory for the NoteIndex and a lightweight fake for the Qdrant
client and PlatformClient, so no Docker infra is needed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_knowledge.db import DocIndex, NoteIndex
from epicurus_knowledge.indexer import KnowledgeIndexer

TENANT = "test"
EMBED_DIM = 4


def _suggestions() -> Any:
    """A fresh in-memory suggestion store for build_module (#220); never exercised here."""
    from epicurus_knowledge.suggestions import SuggestionStore

    return SuggestionStore(create_async_engine("sqlite+aiosqlite:///:memory:"))


def _fake_vectors(texts: list[str]) -> list[list[float]]:
    return [[float(i), 0.0, 0.0, 0.0] for i in range(len(texts))]


def _make_mock_platform(model: str | None = None) -> Any:
    platform = MagicMock()
    platform.embed = AsyncMock(side_effect=lambda texts, **_: _fake_vectors(texts))
    platform.get_module_model = AsyncMock(return_value=model)
    return platform


def _query_response(points: list[Any]) -> Any:
    """Wrap hits as a qdrant ``query_points`` response — results live on ``.points``."""
    return SimpleNamespace(points=points)


def _make_mock_qdrant() -> Any:
    qdrant = MagicMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    qdrant.create_collection = AsyncMock()
    qdrant.upsert = AsyncMock()
    qdrant.delete = AsyncMock()
    qdrant.query_points = AsyncMock(return_value=_query_response([]))
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


async def test_index_path_indexes_a_single_file(note_index: NoteIndex, vault: Path) -> None:
    indexer = _make_indexer(note_index, vault)
    (vault / "note_c.md").write_text("# Note C\n\nFresh content.")
    chunks = await indexer.index_path("note_c.md")
    assert chunks >= 1
    rec = await note_index.get(tenant=TENANT, note_path="note_c.md")
    assert rec is not None
    assert rec.chunk_count == chunks


async def test_index_path_replaces_old_vectors_on_reindex(
    note_index: NoteIndex, vault: Path
) -> None:
    indexer = _make_indexer(note_index, vault)
    await indexer.run()  # note_a is now tracked
    qdrant = indexer._qdrant  # type: ignore[attr-defined]
    qdrant.delete.reset_mock()
    (vault / "note_a.md").write_text("# Note A\n\nEdited via the editor page.")
    await indexer.index_path("note_a.md")
    qdrant.delete.assert_awaited()  # stale chunks purged before the re-upsert


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


async def test_run_uses_selected_embedding_model(note_index: NoteIndex, vault: Path) -> None:
    """The operator's chosen embedding model is resolved and passed to every embed call (#128)."""
    platform = _make_mock_platform(model="nomic-embed-text")
    indexer = KnowledgeIndexer(
        note_index, _make_mock_qdrant(), platform, vault_path=vault, tenant=TENANT
    )
    await indexer.run()
    platform.get_module_model.assert_awaited_with("embedding")
    assert platform.embed.await_args_list  # notes were embedded
    assert all(
        call.kwargs.get("model") == "nomic-embed-text" for call in platform.embed.await_args_list
    )


async def test_run_falls_back_to_core_default_when_unset(
    note_index: NoteIndex, vault: Path
) -> None:
    """An unset slot resolves to None — embed receives ``model=None`` (the core default)."""
    platform = _make_mock_platform(model=None)
    indexer = KnowledgeIndexer(
        note_index, _make_mock_qdrant(), platform, vault_path=vault, tenant=TENANT
    )
    await indexer.run()
    assert platform.embed.await_args_list
    assert all(call.kwargs.get("model") is None for call in platform.embed.await_args_list)


async def test_search_uses_selected_embedding_model(note_index: NoteIndex, vault: Path) -> None:
    """A query is embedded with the operator's chosen model so it matches the index (#128)."""
    platform = _make_mock_platform(model="nomic-embed-text")
    indexer = KnowledgeIndexer(
        note_index, _make_mock_qdrant(), platform, vault_path=vault, tenant=TENANT
    )
    await indexer.search("hello")
    platform.get_module_model.assert_awaited_with("embedding")
    assert platform.embed.await_args.kwargs.get("model") == "nomic-embed-text"


def _make_docs_indexer(tmp_path: Path) -> KnowledgeIndexer:
    """A KnowledgeIndexer configured as the platform-docs source."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")  # type: ignore[attr-defined]
    # We can't await here — callers share the note_index fixture instead.
    doc_index = DocIndex(engine)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "index.md").write_text("# Epicurus Docs\n\nPlatform overview.")
    return KnowledgeIndexer(
        doc_index,
        _make_mock_qdrant(),
        _make_mock_platform(),
        vault_path=docs_dir,
        tenant=TENANT,
        collection_base="docs",
    )


async def test_mcp_tool_reindex(note_index: NoteIndex, vault: Path, tmp_path: Path) -> None:
    from epicurus_knowledge.service import build_module

    vault_indexer = _make_indexer(note_index, vault)
    docs_indexer = _make_docs_indexer(tmp_path)
    await docs_indexer._notes.init()  # type: ignore[attr-defined]
    module_docs_stub = MagicMock()
    module_docs_stub.run = AsyncMock(return_value={"indexed": 0, "deleted": 0, "unchanged": 0})
    module = build_module(
        vault_indexer,
        docs_indexer,
        module_docs_stub,
        _suggestions(),
        tenant=TENANT,
        vault_path=vault,
    )
    _content, structured = await module.mcp.call_tool("knowledge_reindex", {})
    assert isinstance(structured, dict)
    payload: dict[str, object] = structured.get("result") or structured  # type: ignore[assignment]
    assert "indexed" in payload


async def test_manifest_declares_tool_and_event(
    note_index: NoteIndex, vault: Path, tmp_path: Path
) -> None:
    from epicurus_knowledge.service import build_module

    vault_indexer = _make_indexer(note_index, vault)
    docs_indexer = _make_docs_indexer(tmp_path)
    await docs_indexer._notes.init()  # type: ignore[attr-defined]
    module = build_module(
        vault_indexer,
        docs_indexer,
        MagicMock(),
        _suggestions(),
        tenant=TENANT,
        vault_path=vault,
    )
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
    qdrant.query_points = AsyncMock(return_value=_query_response([hit]))

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
    qdrant.query_points = AsyncMock(return_value=_query_response([hit_no_payload]))

    indexer = KnowledgeIndexer(
        note_index,
        qdrant,
        _make_mock_platform(),
        vault_path=vault,
        tenant=TENANT,
    )
    results = await indexer.search("query")
    assert results == []


async def test_mcp_tool_search(note_index: NoteIndex, vault: Path, tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from epicurus_knowledge.service import build_module

    qdrant = _make_mock_qdrant()
    hit = MagicMock()
    hit.score = 0.8
    hit.payload = {"note_path": "note_b.md", "heading": None, "text": "Content of B."}
    qdrant.query_points = AsyncMock(return_value=_query_response([hit]))

    vault_indexer = KnowledgeIndexer(
        note_index,
        qdrant,
        _make_mock_platform(),
        vault_path=vault,
        tenant=TENANT,
    )
    docs_indexer = _make_docs_indexer(tmp_path)
    await docs_indexer._notes.init()  # type: ignore[attr-defined]
    module = build_module(
        vault_indexer,
        docs_indexer,
        MagicMock(),
        _suggestions(),
        tenant=TENANT,
        vault_path=vault,
    )
    _content, structured = await module.mcp.call_tool("knowledge_search", {"query": "B", "k": 1})
    assert structured is not None


async def test_collection_base_scopes_qdrant_collection(note_index: NoteIndex, vault: Path) -> None:
    """collection_base='docs' produces a <tenant>__docs collection, not <tenant>__knowledge."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    doc_idx = DocIndex(engine)
    await doc_idx.init()

    docs_indexer = KnowledgeIndexer(
        doc_idx,
        _make_mock_qdrant(),
        _make_mock_platform(),
        vault_path=vault,
        tenant=TENANT,
        collection_base="docs",
    )
    assert docs_indexer._collection == f"{TENANT}__docs"

    vault_indexer = _make_indexer(note_index, vault)
    assert vault_indexer._collection == f"{TENANT}__knowledge"


async def test_merged_search_returns_hits_from_both_sources(
    note_index: NoteIndex, vault: Path, tmp_path: Path
) -> None:
    """knowledge_search merges vault + docs results ranked by score."""
    from unittest.mock import MagicMock

    from epicurus_knowledge.service import build_module

    vault_qdrant = _make_mock_qdrant()
    vault_hit = MagicMock()
    vault_hit.score = 0.7
    vault_hit.payload = {"note_path": "note_a.md", "heading": None, "text": "Vault content."}
    vault_qdrant.query_points = AsyncMock(return_value=_query_response([vault_hit]))

    docs_qdrant = _make_mock_qdrant()
    docs_hit = MagicMock()
    docs_hit.score = 0.9
    docs_hit.payload = {
        # docs-relative path (no docs/ prefix) — knowledge_search adds the prefix for display.
        "note_path": "services/knowledge.md",
        "heading": "knowledge",
        "text": "Platform docs content.",
    }
    docs_qdrant.query_points = AsyncMock(return_value=_query_response([docs_hit]))

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    doc_idx = DocIndex(engine)
    await doc_idx.init()
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    vault_indexer = KnowledgeIndexer(
        note_index, vault_qdrant, _make_mock_platform(), vault_path=vault, tenant=TENANT
    )
    docs_indexer = KnowledgeIndexer(
        doc_idx,
        docs_qdrant,
        _make_mock_platform(),
        vault_path=docs_dir,
        tenant=TENANT,
        collection_base="docs",
    )

    module_docs_stub = MagicMock()
    module_docs_stub.run = AsyncMock(return_value={"indexed": 0, "deleted": 0, "unchanged": 0})
    module = build_module(
        vault_indexer,
        docs_indexer,
        module_docs_stub,
        _suggestions(),
        tenant=TENANT,
        vault_path=vault,
    )
    from epicurus_core.contracts import ToolEnvelope

    content, _ = await module.mcp.call_tool("knowledge_search", {"query": "platform", "k": 5})
    env = ToolEnvelope.model_validate_json(content[0].text)  # type: ignore[attr-defined]
    # Both chunks' text reaches the model.
    assert "Vault content." in env.text
    assert "Platform docs content." in env.text
    # Docs hit (0.9) ranks above vault hit (0.7): first in the text and as the first chip.
    assert env.text.index("Platform docs content.") < env.text.index("Vault content.")
    assert "docs/services/knowledge.md" in env.text  # docs path is prefixed for the agent
    assert env.entity_refs[0].title == "knowledge"  # the docs hit's heading, ranked first


async def test_reindex_sums_both_sources(
    note_index: NoteIndex, vault: Path, tmp_path: Path
) -> None:
    """knowledge_reindex returns summed counts across vault and docs."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from epicurus_knowledge.service import build_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    doc_idx = DocIndex(engine)
    await doc_idx.init()

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "index.md").write_text("# Docs\n\nContent.")

    vault_indexer = _make_indexer(note_index, vault)
    docs_indexer = KnowledgeIndexer(
        doc_idx,
        _make_mock_qdrant(),
        _make_mock_platform(),
        vault_path=docs_dir,
        tenant=TENANT,
        collection_base="docs",
    )

    module_docs_stub = MagicMock()
    module_docs_stub.run = AsyncMock(return_value={"indexed": 0, "deleted": 0, "unchanged": 0})
    module = build_module(
        vault_indexer,
        docs_indexer,
        module_docs_stub,
        _suggestions(),
        tenant=TENANT,
        vault_path=vault,
    )
    _content, structured = await module.mcp.call_tool("knowledge_reindex", {})
    assert isinstance(structured, dict)
    payload: dict[str, object] = structured.get("result") or structured  # type: ignore[assignment]
    # vault has 2 notes, docs has 1 → total indexed >= 3 on first run
    assert isinstance(payload.get("indexed"), int)
    assert payload["indexed"] >= 3  # type: ignore[operator]


# ── Batched embedding (#230) ──────────────────────────────────────────────────


async def test_run_batches_embeds_across_files(note_index: NoteIndex, tmp_path: Path) -> None:
    """A large batch size embeds every file's chunks in a single platform round-trip."""
    for i in range(5):
        (tmp_path / f"n{i}.md").write_text(f"# Note {i}\n\nBody {i}.")
    platform = _make_mock_platform()
    indexer = KnowledgeIndexer(
        note_index,
        _make_mock_qdrant(),
        platform,
        vault_path=tmp_path,
        tenant=TENANT,
        embed_batch_size=64,
    )
    result = await indexer.run()
    assert result["indexed"] == 5
    # Five files, but one batched embed call — not one call per file.
    assert platform.embed.call_count == 1


async def test_run_one_flush_per_file_when_batch_size_one(
    note_index: NoteIndex, tmp_path: Path
) -> None:
    """A batch size of 1 flushes after each file, so every note triggers its own call."""
    for i in range(5):
        (tmp_path / f"n{i}.md").write_text(f"# Note {i}\n\nBody {i}.")
    platform = _make_mock_platform()
    indexer = KnowledgeIndexer(
        note_index,
        _make_mock_qdrant(),
        platform,
        vault_path=tmp_path,
        tenant=TENANT,
        embed_batch_size=1,
    )
    result = await indexer.run()
    assert result["indexed"] == 5
    assert platform.embed.call_count == 5


async def test_run_persists_every_batched_file(note_index: NoteIndex, tmp_path: Path) -> None:
    """Batched files are all recorded in the ledger and searchable on the next run."""
    for i in range(3):
        (tmp_path / f"n{i}.md").write_text(f"# Note {i}\n\nBody {i}.")
    indexer = KnowledgeIndexer(
        note_index,
        _make_mock_qdrant(),
        _make_mock_platform(),
        vault_path=tmp_path,
        tenant=TENANT,
        embed_batch_size=2,  # forces a mid-walk flush plus a final flush
    )
    await indexer.run()
    assert await note_index.count(tenant=TENANT) == 3
    # A second run sees everything as unchanged — the ledger captured each file.
    second = await indexer.run()
    assert second["unchanged"] == 3
    assert second["indexed"] == 0


# ── Qdrant-reset self-heal (#229) ─────────────────────────────────────────────


def _missing_collection_indexer(note_index: NoteIndex, vault: Path) -> KnowledgeIndexer:
    qdrant = _make_mock_qdrant()
    qdrant.collection_exists = AsyncMock(return_value=False)  # vectors wiped
    return KnowledgeIndexer(
        note_index, qdrant, _make_mock_platform(), vault_path=vault, tenant=TENANT
    )


async def test_reconcile_clears_ledger_when_collection_missing(
    note_index: NoteIndex, vault: Path
) -> None:
    indexer = _missing_collection_indexer(note_index, vault)
    await note_index.upsert(
        tenant=TENANT, note_path="note_a.md", mtime_ns=1, content_hash="h", chunk_count=1
    )
    assert await note_index.count(tenant=TENANT) == 1
    assert await indexer.reconcile() is True
    assert await note_index.count(tenant=TENANT) == 0


async def test_reconcile_noop_when_collection_exists(note_index: NoteIndex, vault: Path) -> None:
    indexer = _make_indexer(note_index, vault)  # collection_exists → True
    await note_index.upsert(
        tenant=TENANT, note_path="note_a.md", mtime_ns=1, content_hash="h", chunk_count=1
    )
    assert await indexer.reconcile() is False
    assert await note_index.count(tenant=TENANT) == 1  # ledger untouched


async def test_reconcile_noop_when_ledger_empty(note_index: NoteIndex, vault: Path) -> None:
    indexer = _missing_collection_indexer(note_index, vault)
    assert await indexer.reconcile() is False


async def test_reindexes_from_scratch_after_qdrant_reset(
    note_index: NoteIndex, vault: Path
) -> None:
    """After a wipe, reconcile + run rebuilds the index even though files are unchanged."""
    indexer = _missing_collection_indexer(note_index, vault)
    await indexer.run()
    assert await note_index.count(tenant=TENANT) == 2  # two seed notes
    # Simulate the qdrant volume being reset out from under us.
    assert await indexer.reconcile() is True
    assert await note_index.count(tenant=TENANT) == 0
    result = await indexer.run()
    assert result["indexed"] == 2  # full re-embed, not skipped as "unchanged"


async def test_clear_removes_all_rows(note_index: NoteIndex) -> None:
    await note_index.upsert(
        tenant=TENANT, note_path="a.md", mtime_ns=1, content_hash="h1", chunk_count=1
    )
    await note_index.upsert(
        tenant=TENANT, note_path="b.md", mtime_ns=2, content_hash="h2", chunk_count=1
    )
    await note_index.clear(tenant=TENANT)
    assert await note_index.count(tenant=TENANT) == 0
