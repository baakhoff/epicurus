"""Schema regression tests and unit tests for the knowledge note and doc indexes."""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_knowledge.db import DocIndex, NoteIndex, _StoredDoc, _StoredNote

TENANT = "test"


def test_mtime_ns_is_bigint_not_int32() -> None:
    # Nanosecond epoch mtimes (~1.8e18) overflow Postgres INTEGER (int32); SQLite's
    # dynamic typing hides this in unit tests, so guard the column type explicitly.
    assert isinstance(_StoredNote.__table__.c.mtime_ns.type, BigInteger)
    assert isinstance(_StoredDoc.__table__.c.mtime_ns.type, BigInteger)


@pytest.fixture
async def index() -> NoteIndex:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx = NoteIndex(engine)
    await idx.init()
    return idx


async def test_count_empty(index: NoteIndex) -> None:
    assert await index.count(tenant=TENANT) == 0


async def test_count_after_upsert(index: NoteIndex) -> None:
    await index.upsert(
        tenant=TENANT, note_path="a.md", mtime_ns=1, content_hash="abc", chunk_count=2
    )
    await index.upsert(
        tenant=TENANT, note_path="b.md", mtime_ns=2, content_hash="def", chunk_count=1
    )
    assert await index.count(tenant=TENANT) == 2


async def test_count_is_tenant_scoped(index: NoteIndex) -> None:
    await index.upsert(
        tenant="tenant-a", note_path="x.md", mtime_ns=1, content_hash="aaa", chunk_count=1
    )
    assert await index.count(tenant="tenant-a") == 1
    assert await index.count(tenant="tenant-b") == 0


async def test_last_indexed_at_empty(index: NoteIndex) -> None:
    assert await index.last_indexed_at(tenant=TENANT) is None


async def test_last_indexed_at_returns_iso_string(index: NoteIndex) -> None:
    await index.upsert(
        tenant=TENANT, note_path="note.md", mtime_ns=1, content_hash="abc", chunk_count=3
    )
    result = await index.last_indexed_at(tenant=TENANT)
    assert result is not None
    # Must be parseable as ISO-8601.
    from datetime import datetime

    datetime.fromisoformat(result)


async def test_count_decrements_on_delete(index: NoteIndex) -> None:
    await index.upsert(
        tenant=TENANT, note_path="del.md", mtime_ns=1, content_hash="xyz", chunk_count=1
    )
    assert await index.count(tenant=TENANT) == 1
    await index.delete(tenant=TENANT, note_path="del.md")
    assert await index.count(tenant=TENANT) == 0


# ── DocIndex ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def doc_index() -> DocIndex:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    idx = DocIndex(engine)
    await idx.init()
    return idx


async def test_doc_index_count_empty(doc_index: DocIndex) -> None:
    assert await doc_index.count(tenant=TENANT) == 0


async def test_doc_index_upsert_and_count(doc_index: DocIndex) -> None:
    await doc_index.upsert(
        tenant=TENANT,
        note_path="docs/services/knowledge.md",
        mtime_ns=1,
        content_hash="abc",
        chunk_count=3,
    )
    assert await doc_index.count(tenant=TENANT) == 1


async def test_doc_index_tenant_isolated_from_note_index(doc_index: DocIndex) -> None:
    """DocIndex uses a separate table — NoteIndex rows must not bleed into DocIndex."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    note_idx = NoteIndex(engine)
    doc_idx = DocIndex(engine)
    await note_idx.init()
    await doc_idx.init()

    await note_idx.upsert(
        tenant=TENANT, note_path="index.md", mtime_ns=1, content_hash="abc", chunk_count=1
    )
    # DocIndex must not see the NoteIndex row even though note_path collides.
    assert await doc_idx.count(tenant=TENANT) == 0
    assert await doc_idx.get(tenant=TENANT, note_path="index.md") is None


async def test_doc_index_delete(doc_index: DocIndex) -> None:
    await doc_index.upsert(
        tenant=TENANT, note_path="docs/index.md", mtime_ns=1, content_hash="abc", chunk_count=2
    )
    await doc_index.delete(tenant=TENANT, note_path="docs/index.md")
    assert await doc_index.count(tenant=TENANT) == 0


async def test_doc_index_list_paths(doc_index: DocIndex) -> None:
    await doc_index.upsert(
        tenant=TENANT, note_path="docs/a.md", mtime_ns=1, content_hash="a", chunk_count=1
    )
    await doc_index.upsert(
        tenant=TENANT, note_path="docs/b.md", mtime_ns=2, content_hash="b", chunk_count=1
    )
    paths = await doc_index.list_paths(tenant=TENANT)
    assert set(paths) == {"docs/a.md", "docs/b.md"}
