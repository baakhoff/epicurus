"""Unit tests for the tenant-scoped notes store."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_notes.db import NoteFolderStore, NotesStore

TENANT = "test"


@pytest.fixture
async def store() -> NotesStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    s = NotesStore(engine)
    await s.init()
    return s


@pytest.fixture
async def folders() -> NoteFolderStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    f = NoteFolderStore(engine)
    await f.init()
    return f


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


# ── NoteFolderStore (#KB-refactor) ─────────────────────────────────────────────


async def test_folder_add_is_idempotent_and_lists_sorted(folders: NoteFolderStore) -> None:
    assert await folders.add(tenant=TENANT, path="b") is True
    assert await folders.add(tenant=TENANT, path="a/c") is True
    assert await folders.add(tenant=TENANT, path="a") is True
    # A second add of the same path is a no-op (False) — the unique row already exists.
    assert await folders.add(tenant=TENANT, path="b") is False
    # Sorted so a parent ("a") precedes its child ("a/c").
    assert await folders.list(tenant=TENANT) == ["a", "a/c", "b"]


async def test_folder_delete_and_tenant_isolation(folders: NoteFolderStore) -> None:
    await folders.add(tenant="t1", path="shared")
    await folders.add(tenant="t2", path="shared")
    assert await folders.delete(tenant="t1", path="shared") is True
    assert await folders.list(tenant="t1") == []
    # t2's identically-named folder is untouched.
    assert await folders.list(tenant="t2") == ["shared"]
    # Deleting a missing folder is a no-op (False).
    assert await folders.delete(tenant="t1", path="shared") is False
