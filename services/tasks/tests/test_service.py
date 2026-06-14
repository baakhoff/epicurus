"""Unit tests for the tasks module tool surface via the LocalTasksProvider."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import CONTRACT_VERSION
from epicurus_tasks.db import TaskStore
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.service import build_module

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
    assert manifest.version == "0.2.0"
    assert manifest.contract_version == CONTRACT_VERSION
    tool_names = {t.name for t in manifest.tools}
    assert tool_names == {"tasks_list", "tasks_add", "tasks_complete", "tasks_update"}
    # The Tasks left-nav page is declared as a core `board` archetype (ADR-0018).
    pages = {p.id: p for p in manifest.pages}
    assert pages["board"].archetype == "board"
    assert pages["board"].title == "Tasks"


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
