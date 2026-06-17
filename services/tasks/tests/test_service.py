"""Unit tests for the tasks module tool surface via the LocalTasksProvider."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import CONTRACT_VERSION
from epicurus_tasks.db import TaskStore
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.models import Task
from epicurus_tasks.service import (
    TaskNotFound,
    build_module,
    fetch_task,
    task_attachment,
    task_attachment_item,
    task_excerpt,
    tasks_attachments,
)

TENANT = "test-tenant"


@pytest.fixture()
async def module_fixture() -> object:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = TaskStore(engine)
    await store.init()
    provider = LocalTasksProvider(store)
    return build_module(provider, tenant_id=TENANT)


async def test_manifest(module_fixture: object) -> None:
    mod = module_fixture
    manifest = await mod.manifest()  # type: ignore[attr-defined]
    assert manifest.name == "tasks"
    assert manifest.version == "0.3.0"
    assert manifest.contract_version == CONTRACT_VERSION
    tool_names = {t.name for t in manifest.tools}
    assert tool_names == {"tasks_list", "tasks_add", "tasks_complete", "tasks_update"}
    # The Tasks left-nav page is declared as a core `board` archetype (ADR-0018).
    pages = {p.id: p for p in manifest.pages}
    assert pages["board"].archetype == "board"
    assert pages["board"].title == "Tasks"
    # Tasks is a chat-attachment source (ADR-0019).
    assert manifest.attachable is True


async def test_tasks_list_empty(module_fixture: object) -> None:
    mod = module_fixture
    _, structured = await mod.mcp.call_tool("tasks_list", {})  # type: ignore[attr-defined]
    # list return → {"result": [...]}
    assert structured == {"result": []}


async def test_tasks_add_and_list(module_fixture: object) -> None:
    mod = module_fixture
    # Pydantic model return → model dict directly (not wrapped in "result")
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "Deploy to prod", "due": "2025-12-31"}
    )
    assert task["title"] == "Deploy to prod"
    assert task["due"] == "2025-12-31"
    assert not task["completed"]

    _, list_result = await mod.mcp.call_tool("tasks_list", {})  # type: ignore[attr-defined]
    tasks = list_result["result"]
    assert len(tasks) == 1
    assert tasks[0]["id"] == task["id"]


async def test_tasks_complete(module_fixture: object) -> None:
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "Fix the bug"}
    )
    task_id = task["id"]

    _, done = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_complete", {"task_id": task_id}
    )
    assert done["completed"]

    _, list_result = await mod.mcp.call_tool("tasks_list", {})  # type: ignore[attr-defined]
    assert all(t["id"] != task_id for t in list_result["result"])


async def test_complete_nonexistent_raises(module_fixture: object) -> None:
    mod = module_fixture
    with pytest.raises(Exception, match="not found"):
        await mod.mcp.call_tool("tasks_complete", {"task_id": "bad-id"})  # type: ignore[attr-defined]


async def test_tasks_update(module_fixture: object) -> None:
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "Draft", "notes": "rough"}
    )
    task_id = task["id"]

    _, updated = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_update", {"task_id": task_id, "title": "Final", "due": "2026-01-01"}
    )
    assert updated["title"] == "Final"
    assert updated["due"] == "2026-01-01"
    assert updated["notes"] == "rough"  # untouched field is preserved


async def test_update_nonexistent_raises(module_fixture: object) -> None:
    mod = module_fixture
    with pytest.raises(Exception, match="not found"):
        await mod.mcp.call_tool(  # type: ignore[attr-defined]
            "tasks_update", {"task_id": "bad-id", "title": "x"}
        )


# ── Chat-attachment source helpers (ADR-0019) ─────────────────────────────────


@pytest.fixture()
async def local_provider() -> LocalTasksProvider:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = TaskStore(engine)
    await store.init()
    return LocalTasksProvider(store)


def _task(**kw: Any) -> Task:
    base: dict[str, Any] = {
        "id": "t1",
        "title": "Write report",
        "notes": None,
        "due": None,
        "completed": False,
        "completed_at": None,
    }
    base.update(kw)
    return Task(**base)


def test_task_excerpt_includes_due_status_and_notes() -> None:
    excerpt = task_excerpt(_task(due="2026-06-20", notes="Q2 numbers"))
    assert "Write report" in excerpt
    assert "2026-06-20" in excerpt
    assert "Q2 numbers" in excerpt
    assert "Open" in excerpt


def test_task_excerpt_marks_completed() -> None:
    assert "Completed" in task_excerpt(_task(completed=True))


def test_task_attachment_item_shape() -> None:
    assert task_attachment_item(_task()) == {
        "ref_id": "t1",
        "kind": "task",
        "title": "Write report",
    }


def test_task_attachment_payload_has_title_and_excerpt() -> None:
    payload = task_attachment(_task(notes="details here"))
    assert payload["title"] == "Write report"
    assert "details here" in payload["excerpt"]


async def test_fetch_task_returns_task(local_provider: LocalTasksProvider) -> None:
    created = await local_provider.add_task(TENANT, "Fetch me")
    fetched = await fetch_task(local_provider, tenant_id=TENANT, ref_id=created.id)
    assert fetched.id == created.id
    assert fetched.title == "Fetch me"


async def test_fetch_task_missing_raises(local_provider: LocalTasksProvider) -> None:
    with pytest.raises(TaskNotFound):
        await fetch_task(local_provider, tenant_id=TENANT, ref_id="does-not-exist")


async def test_tasks_attachments_lists_open_tasks(local_provider: LocalTasksProvider) -> None:
    a = await local_provider.add_task(TENANT, "A")
    await local_provider.add_task(TENANT, "B")
    items = await tasks_attachments(local_provider, tenant_id=TENANT)
    titles = [i["title"] for i in items]
    assert "A" in titles
    assert "B" in titles
    assert all(i["kind"] == "task" for i in items)
    assert any(i["ref_id"] == a.id for i in items)


async def test_tasks_attachments_respects_limit(local_provider: LocalTasksProvider) -> None:
    for n in range(5):
        await local_provider.add_task(TENANT, f"T{n}")
    items = await tasks_attachments(local_provider, tenant_id=TENANT, limit=3)
    assert len(items) == 3
