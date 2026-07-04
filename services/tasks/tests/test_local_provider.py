"""Tests for LocalTasksProvider backed by an in-memory SQLite database."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_tasks.db import TaskStore
from epicurus_tasks.local_provider import LocalTasksProvider

TENANT = "test-tenant"


@pytest.fixture()
async def store() -> TaskStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    ts = TaskStore(engine)
    await ts.init()
    return ts


@pytest.fixture()
def provider(store: TaskStore) -> LocalTasksProvider:
    return LocalTasksProvider(store)


async def test_provider_name(provider: LocalTasksProvider) -> None:
    assert provider.provider_name() == "local"


async def test_list_empty(provider: LocalTasksProvider) -> None:
    tasks = await provider.list_tasks(TENANT)
    assert tasks == []


async def test_add_and_list(provider: LocalTasksProvider) -> None:
    task = await provider.add_task(TENANT, "Buy milk", notes="2 litres", due="2025-12-01")
    assert task.title == "Buy milk"
    assert task.notes == "2 litres"
    assert task.due == "2025-12-01"
    assert not task.completed
    assert task.id  # UUID assigned

    tasks = await provider.list_tasks(TENANT)
    assert len(tasks) == 1
    assert tasks[0].id == task.id


async def test_add_minimal(provider: LocalTasksProvider) -> None:
    task = await provider.add_task(TENANT, "Minimal task")
    assert task.title == "Minimal task"
    assert task.notes is None
    assert task.due is None


async def test_complete_task(provider: LocalTasksProvider) -> None:
    task = await provider.add_task(TENANT, "Finish report")
    done = await provider.complete_task(TENANT, task.id)
    assert done.completed
    assert done.completed_at is not None

    # Completed task should not appear in list (list only shows open tasks).
    tasks = await provider.list_tasks(TENANT)
    assert all(t.id != task.id for t in tasks)


async def test_complete_unknown_raises(provider: LocalTasksProvider) -> None:
    with pytest.raises(ValueError, match="not found"):
        await provider.complete_task(TENANT, "nonexistent-id")


async def test_list_scope_selects_open_done_or_all(provider: LocalTasksProvider) -> None:
    """scope filters the read: open (default) / done / all (ADR-0049)."""
    open_t = await provider.add_task(TENANT, "Still open")
    done_t = await provider.add_task(TENANT, "Finished")
    await provider.complete_task(TENANT, done_t.id)

    open_only = await provider.list_tasks(TENANT)  # default scope="open"
    assert [t.id for t in open_only] == [open_t.id]

    done_only = await provider.list_tasks(TENANT, scope="done")
    assert [t.id for t in done_only] == [done_t.id]
    assert done_only[0].completed

    everything = await provider.list_tasks(TENANT, scope="all")
    assert {t.id for t in everything} == {open_t.id, done_t.id}


async def test_tenant_isolation(provider: LocalTasksProvider) -> None:
    await provider.add_task("tenant-a", "Task A")
    await provider.add_task("tenant-b", "Task B")

    a_tasks = await provider.list_tasks("tenant-a")
    b_tasks = await provider.list_tasks("tenant-b")

    assert len(a_tasks) == 1
    assert a_tasks[0].title == "Task A"
    assert len(b_tasks) == 1
    assert b_tasks[0].title == "Task B"


async def test_list_id_ignored(provider: LocalTasksProvider) -> None:
    """list_id is silently ignored by the local provider — single flat list."""
    await provider.add_task(TENANT, "Task")
    tasks = await provider.list_tasks(TENANT, list_id="some-list-id")
    assert len(tasks) == 1


async def test_update_task(provider: LocalTasksProvider) -> None:
    task = await provider.add_task(TENANT, "Old title", notes="old", due="2025-01-01")
    updated = await provider.update_task(TENANT, task.id, title="New title", due="2025-02-02")
    assert updated.title == "New title"
    assert updated.due == "2025-02-02"
    assert updated.notes == "old"  # not passed → unchanged


async def test_update_task_partial(provider: LocalTasksProvider) -> None:
    """Only the supplied field changes; the others are left intact."""
    task = await provider.add_task(TENANT, "Keep title")
    updated = await provider.update_task(TENANT, task.id, notes="added notes")
    assert updated.title == "Keep title"
    assert updated.notes == "added notes"


async def test_update_task_noop_returns_current(provider: LocalTasksProvider) -> None:
    task = await provider.add_task(TENANT, "Unchanged", notes="n")
    same = await provider.update_task(TENANT, task.id)
    assert same.title == "Unchanged"
    assert same.notes == "n"


async def test_update_unknown_raises(provider: LocalTasksProvider) -> None:
    with pytest.raises(ValueError, match="not found"):
        await provider.update_task(TENANT, "nonexistent-id", title="x")


# ── Clear sentinel: due="" / notes="" unsets the field (#475) ─────────────────


async def test_update_task_clears_due_with_empty_string(provider: LocalTasksProvider) -> None:
    task = await provider.add_task(TENANT, "Has a date", due="2025-01-01")
    updated = await provider.update_task(TENANT, task.id, due="")
    assert updated.due is None


async def test_update_task_clears_notes_with_empty_string(provider: LocalTasksProvider) -> None:
    task = await provider.add_task(TENANT, "Has notes", notes="some notes")
    updated = await provider.update_task(TENANT, task.id, notes="")
    assert updated.notes is None


async def test_update_task_clear_due_leaves_other_fields_untouched(
    provider: LocalTasksProvider,
) -> None:
    task = await provider.add_task(TENANT, "Keep title", notes="keep", due="2025-01-01")
    updated = await provider.update_task(TENANT, task.id, due="")
    assert updated.title == "Keep title"
    assert updated.notes == "keep"
    assert updated.due is None


async def test_get_task_returns_task(provider: LocalTasksProvider) -> None:
    """get_task backs the resolver / attachment source (ADR-0019)."""
    task = await provider.add_task(TENANT, "Find me", notes="here")
    fetched = await provider.get_task(TENANT, task.id)
    assert fetched is not None
    assert fetched.id == task.id
    assert fetched.title == "Find me"


async def test_get_task_missing_returns_none(provider: LocalTasksProvider) -> None:
    assert await provider.get_task(TENANT, "nonexistent-id") is None


async def test_create_list_not_implemented(provider: LocalTasksProvider) -> None:
    """The local store is a single implicit list — it has nothing to create (#474)."""
    with pytest.raises(NotImplementedError, match="connect Google"):
        await provider.create_list(TENANT, "Groceries")
