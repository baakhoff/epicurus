"""Unit tests for the tenant-scoped notes store."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_notes.db import NotesStore

TENANT = "test"


@pytest.fixture
async def store() -> NotesStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    s = NotesStore(engine)
    await s.init()
    return s


async def test_count_empty(store: NotesStore) -> None:
    assert await store.count(tenant=TENANT) == 0
    assert await store.last_updated_at(tenant=TENANT) is None


async def test_upsert_creates_then_updates(store: NotesStore) -> None:
    created = await store.upsert(tenant=TENANT, slug="a", title="A", content="one")
    assert created.title == "A"
    assert created.content == "one"
    assert await store.count(tenant=TENANT) == 1

    updated = await store.upsert(tenant=TENANT, slug="a", title="A2", content="two")
    assert updated.content == "two"
    assert updated.title == "A2"
    # Same slug → still one row.
    assert await store.count(tenant=TENANT) == 1


async def test_get_returns_none_for_missing(store: NotesStore) -> None:
    assert await store.get(tenant=TENANT, slug="ghost") is None


async def test_list_summaries_excludes_other_tenants(store: NotesStore) -> None:
    await store.upsert(tenant="tenant-a", slug="x", title="X", content="x")
    await store.upsert(tenant="tenant-b", slug="y", title="Y", content="y")
    a = await store.list_summaries(tenant="tenant-a")
    assert [s.slug for s in a] == ["x"]
    assert await store.count(tenant="tenant-b") == 1


async def test_delete_removes_row(store: NotesStore) -> None:
    await store.upsert(tenant=TENANT, slug="d", title="D", content="d")
    assert await store.delete(tenant=TENANT, slug="d") is True
    assert await store.count(tenant=TENANT) == 0
    # Deleting a missing slug is a no-op (False).
    assert await store.delete(tenant=TENANT, slug="d") is False


async def test_last_updated_at_is_iso(store: NotesStore) -> None:
    await store.upsert(tenant=TENANT, slug="a", title="A", content="a")
    result = await store.last_updated_at(tenant=TENANT)
    assert result is not None
    from datetime import datetime

    datetime.fromisoformat(result)
