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


# ── version history (ADR-0046) ────────────────────────────────────────────────


async def test_add_version_records_each_distinct_save_newest_first(store: NotesStore) -> None:
    await store.add_version(tenant=TENANT, slug="a", title="One", content="one")
    await store.add_version(tenant=TENANT, slug="a", title="Two", content="two")
    await store.add_version(tenant=TENANT, slug="a", title="Three", content="three")

    versions = await store.list_versions(tenant=TENANT, slug="a")
    assert [v.title for v in versions] == ["Three", "Two", "One"]
    # size is the body length, not a stored body.
    assert [v.size for v in versions] == [len("three"), len("two"), len("one")]
    # version_id is the stringified row PK, monotonically increasing with insert order.
    ids = [int(v.version_id) for v in versions]
    assert ids == sorted(ids, reverse=True)


async def test_add_version_dedups_byte_identical_resave(store: NotesStore) -> None:
    await store.add_version(tenant=TENANT, slug="a", title="A", content="same")
    await store.add_version(tenant=TENANT, slug="a", title="A", content="same")
    assert len(await store.list_versions(tenant=TENANT, slug="a")) == 1
    # A changed body does record a new version; re-saving that again dedups.
    await store.add_version(tenant=TENANT, slug="a", title="A", content="changed")
    await store.add_version(tenant=TENANT, slug="a", title="A", content="changed")
    assert len(await store.list_versions(tenant=TENANT, slug="a")) == 2


async def test_add_version_retains_only_newest_max(store: NotesStore) -> None:
    from epicurus_notes.db import MAX_VERSIONS

    total = MAX_VERSIONS + 10
    for i in range(total):
        await store.add_version(tenant=TENANT, slug="a", title=f"v{i}", content=f"body-{i}")

    versions = await store.list_versions(tenant=TENANT, slug="a")
    assert len(versions) == MAX_VERSIONS
    # The newest MAX_VERSIONS survive; the oldest 10 are pruned.
    assert versions[0].title == f"v{total - 1}"
    assert versions[-1].title == f"v{total - MAX_VERSIONS}"


async def test_get_version_returns_full_content(store: NotesStore) -> None:
    await store.add_version(tenant=TENANT, slug="a", title="A", content="full body here")
    [summary] = await store.list_versions(tenant=TENANT, slug="a")
    fetched = await store.get_version(tenant=TENANT, slug="a", version_id=summary.version_id)
    assert fetched is not None
    assert fetched.content == "full body here"
    assert fetched.title == "A"
    assert fetched.version_id == summary.version_id


async def test_get_version_unknown_or_garbage_is_none(store: NotesStore) -> None:
    await store.add_version(tenant=TENANT, slug="a", title="A", content="x")
    assert await store.get_version(tenant=TENANT, slug="a", version_id="999999") is None
    assert await store.get_version(tenant=TENANT, slug="a", version_id="not-an-int") is None
    assert await store.get_version(tenant=TENANT, slug="a", version_id="") is None


async def test_get_version_is_tenant_scoped(store: NotesStore) -> None:
    await store.add_version(tenant="tenant-a", slug="a", title="A", content="secret")
    [v] = await store.list_versions(tenant="tenant-a", slug="a")
    # tenant-b cannot read tenant-a's version by id, and sees no versions of its own.
    assert await store.get_version(tenant="tenant-b", slug="a", version_id=v.version_id) is None
    assert await store.list_versions(tenant="tenant-b", slug="a") == []


async def test_list_versions_is_slug_scoped(store: NotesStore) -> None:
    await store.add_version(tenant=TENANT, slug="a", title="A", content="a-body")
    await store.add_version(tenant=TENANT, slug="b", title="B", content="b-body")
    a = await store.list_versions(tenant=TENANT, slug="a")
    assert [v.title for v in a] == ["A"]
    # A version of "a" is not fetchable under slug "b".
    assert await store.get_version(tenant=TENANT, slug="b", version_id=a[0].version_id) is None
