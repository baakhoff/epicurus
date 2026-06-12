"""Schema regression tests and unit tests for the knowledge note index."""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_knowledge.db import NoteIndex, _StoredNote

TENANT = "test"


def test_mtime_ns_is_bigint_not_int32() -> None:
    # Nanosecond epoch mtimes (~1.8e18) overflow Postgres INTEGER (int32); SQLite's
    # dynamic typing hides this in unit tests, so guard the column type explicitly.
    assert isinstance(_StoredNote.__table__.c.mtime_ns.type, BigInteger)


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
