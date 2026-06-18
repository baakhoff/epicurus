"""Unit tests for ModuleDocLedger and ModuleDocsIndexer (#215)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_knowledge.module_docs import ModuleDocLedger, ModuleDocsIndexer

TENANT = "test"


# ── Ledger ────────────────────────────────────────────────────────────────────


@pytest.fixture
async def ledger() -> ModuleDocLedger:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    ldr = ModuleDocLedger(engine)
    await ldr.init()
    return ldr


async def test_ledger_empty_by_default(ledger: ModuleDocLedger) -> None:
    assert await ledger.count(tenant=TENANT) == 0
    assert await ledger.list_modules(tenant=TENANT) == []


async def test_ledger_upsert_and_get(ledger: ModuleDocLedger) -> None:
    await ledger.upsert(
        tenant=TENANT,
        module_name="echo",
        doc_path="overview.md",
        content_hash="abc123",
        chunk_count=3,
    )
    h = await ledger.get_hash(tenant=TENANT, module_name="echo", doc_path="overview.md")
    assert h == "abc123"
    assert await ledger.count(tenant=TENANT) == 1


async def test_ledger_upsert_replaces_on_change(ledger: ModuleDocLedger) -> None:
    await ledger.upsert(
        tenant=TENANT, module_name="echo", doc_path="a.md", content_hash="v1", chunk_count=1
    )
    await ledger.upsert(
        tenant=TENANT, module_name="echo", doc_path="a.md", content_hash="v2", chunk_count=2
    )
    h = await ledger.get_hash(tenant=TENANT, module_name="echo", doc_path="a.md")
    assert h == "v2"
    assert await ledger.count(tenant=TENANT) == 1


async def test_ledger_list_paths(ledger: ModuleDocLedger) -> None:
    await ledger.upsert(
        tenant=TENANT, module_name="echo", doc_path="a.md", content_hash="h1", chunk_count=1
    )
    await ledger.upsert(
        tenant=TENANT, module_name="echo", doc_path="b.md", content_hash="h2", chunk_count=1
    )
    assert sorted(await ledger.list_paths(tenant=TENANT, module_name="echo")) == ["a.md", "b.md"]


async def test_ledger_list_modules(ledger: ModuleDocLedger) -> None:
    await ledger.upsert(
        tenant=TENANT, module_name="echo", doc_path="a.md", content_hash="h1", chunk_count=1
    )
    await ledger.upsert(
        tenant=TENANT, module_name="knowledge", doc_path="b.md", content_hash="h2", chunk_count=1
    )
    assert sorted(await ledger.list_modules(tenant=TENANT)) == ["echo", "knowledge"]


async def test_ledger_delete_doc(ledger: ModuleDocLedger) -> None:
    await ledger.upsert(
        tenant=TENANT, module_name="echo", doc_path="a.md", content_hash="h1", chunk_count=1
    )
    await ledger.delete_doc(tenant=TENANT, module_name="echo", doc_path="a.md")
    assert await ledger.get_hash(tenant=TENANT, module_name="echo", doc_path="a.md") is None
    assert await ledger.count(tenant=TENANT) == 0


async def test_ledger_delete_module(ledger: ModuleDocLedger) -> None:
    await ledger.upsert(
        tenant=TENANT, module_name="echo", doc_path="a.md", content_hash="h1", chunk_count=1
    )
    await ledger.upsert(
        tenant=TENANT, module_name="echo", doc_path="b.md", content_hash="h2", chunk_count=1
    )
    await ledger.upsert(
        tenant=TENANT, module_name="other", doc_path="c.md", content_hash="h3", chunk_count=1
    )
    await ledger.delete_module(tenant=TENANT, module_name="echo")
    assert await ledger.count(tenant=TENANT) == 1
    assert await ledger.list_modules(tenant=TENANT) == ["other"]


async def test_ledger_tenant_isolation(ledger: ModuleDocLedger) -> None:
    await ledger.upsert(
        tenant="t1", module_name="echo", doc_path="a.md", content_hash="h1", chunk_count=1
    )
    await ledger.upsert(
        tenant="t2", module_name="echo", doc_path="a.md", content_hash="h2", chunk_count=1
    )
    assert await ledger.get_hash(tenant="t1", module_name="echo", doc_path="a.md") == "h1"
    assert await ledger.get_hash(tenant="t2", module_name="echo", doc_path="a.md") == "h2"
    assert await ledger.count(tenant="t1") == 1
    assert await ledger.count(tenant="t2") == 1


# ── Indexer ───────────────────────────────────────────────────────────────────


def _make_indexer(
    ledger: ModuleDocLedger,
    *,
    snapshots: list[dict[str, Any]],
    module_docs: dict[str, list[dict[str, Any]]],
    embed_vectors: list[list[float]] | None = None,
) -> ModuleDocsIndexer:
    """Build a ModuleDocsIndexer with mocked Qdrant and PlatformClient."""
    if embed_vectors is None:
        embed_vectors = [[0.1, 0.2, 0.3]]

    platform = MagicMock()
    platform.list_modules = AsyncMock(return_value=snapshots)
    platform.get_module_docs = AsyncMock(side_effect=lambda name: module_docs.get(name, []))
    platform.get_module_model = AsyncMock(return_value=None)
    platform.embed = AsyncMock(return_value=embed_vectors)

    qdrant = MagicMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    qdrant.create_collection = AsyncMock()
    qdrant.upsert = AsyncMock()
    qdrant.delete = AsyncMock()

    return ModuleDocsIndexer(
        ledger,
        qdrant,
        platform,
        tenant=TENANT,
        chunk_max_chars=2000,
    )


def _snap(
    name: str, *, enabled: bool = True, docs_url: str | None = "/module-docs"
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "removed": False,
        "manifest": {"name": name, "docs_url": docs_url},
    }


async def test_indexer_indexes_enabled_module_docs(ledger: ModuleDocLedger) -> None:
    indexer = _make_indexer(
        ledger,
        snapshots=[_snap("echo")],
        module_docs={"echo": [{"path": "overview.md", "content": "# Echo\nHello."}]},
    )
    result = await indexer.run()
    assert result["indexed"] == 1
    assert result["unchanged"] == 0
    assert await ledger.count(tenant=TENANT) == 1


async def test_indexer_skips_unchanged_content(ledger: ModuleDocLedger) -> None:
    indexer = _make_indexer(
        ledger,
        snapshots=[_snap("echo")],
        module_docs={"echo": [{"path": "overview.md", "content": "# Echo\nHello."}]},
    )
    await indexer.run()
    result = await indexer.run()  # second run — same content
    assert result["indexed"] == 0
    assert result["unchanged"] == 1


async def test_indexer_skips_disabled_module(ledger: ModuleDocLedger) -> None:
    indexer = _make_indexer(
        ledger,
        snapshots=[_snap("echo", enabled=False)],
        module_docs={"echo": [{"path": "overview.md", "content": "# Echo"}]},
    )
    result = await indexer.run()
    assert result["indexed"] == 0
    assert await ledger.count(tenant=TENANT) == 0


async def test_indexer_skips_module_without_docs_url(ledger: ModuleDocLedger) -> None:
    indexer = _make_indexer(
        ledger,
        snapshots=[_snap("echo", docs_url=None)],
        module_docs={"echo": [{"path": "overview.md", "content": "# Echo"}]},
    )
    result = await indexer.run()
    assert result["indexed"] == 0


async def test_indexer_purges_disabled_module_docs(ledger: ModuleDocLedger) -> None:
    # First run: echo is enabled and has docs.
    indexer = _make_indexer(
        ledger,
        snapshots=[_snap("echo")],
        module_docs={"echo": [{"path": "overview.md", "content": "# Echo"}]},
    )
    await indexer.run()
    assert await ledger.count(tenant=TENANT) == 1

    # Second run: echo is disabled — its docs should be purged.
    indexer2 = _make_indexer(
        ledger,
        snapshots=[_snap("echo", enabled=False)],
        module_docs={},
    )
    result = await indexer2.run()
    assert result["deleted"] == 1
    assert await ledger.count(tenant=TENANT) == 0


async def test_indexer_purges_removed_doc(ledger: ModuleDocLedger) -> None:
    # First run: two docs.
    indexer = _make_indexer(
        ledger,
        snapshots=[_snap("echo")],
        module_docs={
            "echo": [
                {"path": "a.md", "content": "# A"},
                {"path": "b.md", "content": "# B"},
            ]
        },
    )
    await indexer.run()

    # Second run: only one doc remains.
    indexer2 = _make_indexer(
        ledger,
        snapshots=[_snap("echo")],
        module_docs={"echo": [{"path": "a.md", "content": "# A"}]},
    )
    result = await indexer2.run()
    assert result["deleted"] == 1
    assert await ledger.count(tenant=TENANT) == 1


async def test_indexer_recovers_when_list_modules_fails(ledger: ModuleDocLedger) -> None:
    indexer = _make_indexer(ledger, snapshots=[], module_docs={})
    indexer._platform.list_modules = AsyncMock(side_effect=RuntimeError("unreachable"))
    result = await indexer.run()
    # Should return zeros, not raise.
    assert result == {"indexed": 0, "deleted": 0, "unchanged": 0}


async def test_indexer_skips_module_when_fetch_fails(ledger: ModuleDocLedger) -> None:
    indexer = _make_indexer(
        ledger,
        snapshots=[_snap("echo"), _snap("knowledge")],
        module_docs={"knowledge": [{"path": "usage.md", "content": "# Usage"}]},
    )

    # echo fetch raises; knowledge should still be indexed.
    async def _get_docs(name: str) -> list[dict[str, Any]]:
        if name == "echo":
            raise RuntimeError("connection refused")
        return [{"path": "usage.md", "content": "# Usage"}]

    indexer._platform.get_module_docs = _get_docs
    result = await indexer.run()
    assert result["indexed"] == 1
    assert await ledger.count(tenant=TENANT) == 1


async def test_indexer_doc_count(ledger: ModuleDocLedger) -> None:
    indexer = _make_indexer(
        ledger,
        snapshots=[_snap("echo")],
        module_docs={
            "echo": [{"path": "a.md", "content": "# A"}, {"path": "b.md", "content": "# B"}]
        },
    )
    await indexer.run()
    assert await indexer.doc_count() == 2
