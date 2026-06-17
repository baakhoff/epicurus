"""Unit tests for the tasks module tool surface via the LocalTasksProvider."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import CONTRACT_VERSION
from epicurus_core.contracts import ToolEnvelope
from epicurus_tasks.db import TaskStore
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.models import Task
from epicurus_tasks.service import (
    TASK_KIND,
    TaskNotFound,
    build_module,
    fetch_task,
    task_attachment,
    task_attachment_item,
    task_entity_ref,
    task_excerpt,
    task_hover_card,
    tasks_attachments,
)

TENANT = "test-tenant"


def _parse_envelope(content: list[Any]) -> ToolEnvelope:
    """Parse the ToolEnvelope from the first text-content item of a call_tool result."""
    return ToolEnvelope.model_validate_json(content[0].text)


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
    assert manifest.version == "0.4.0"
    assert manifest.contract_version == CONTRACT_VERSION
    tool_names = {t.name for t in manifest.tools}
    assert tool_names == {"tasks_list", "tasks_add", "tasks_complete", "tasks_update"}
    # The Tasks left-nav page is declared as a core `board` archetype (ADR-0018).
    pages = {p.id: p for p in manifest.pages}
    assert pages["board"].archetype == "board"
    assert pages["board"].title == "Tasks"
    # Tasks references tasks in chat (resolver) and is a chat-attachment source (ADR-0019).
    assert manifest.resolver is True
    assert manifest.attachable is True
    # `tasks_list` returns an entity-ref envelope (chips), so it is no longer a card action.
    assert manifest.ui is not None
    assert manifest.ui.actions == []


async def test_tasks_list_empty(module_fixture: object) -> None:
    mod = module_fixture
    content, _ = await mod.mcp.call_tool("tasks_list", {})  # type: ignore[attr-defined]
    envelope = _parse_envelope(content)
    assert envelope.entity_refs == []
    assert "No open tasks" in envelope.text


async def test_tasks_add_and_list(module_fixture: object) -> None:
    mod = module_fixture
    # Pydantic model return → model dict directly (not wrapped in "result")
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "Deploy to prod", "due": "2025-12-31"}
    )
    assert task["title"] == "Deploy to prod"
    assert task["due"] == "2025-12-31"
    assert not task["completed"]

    content, _ = await mod.mcp.call_tool("tasks_list", {})  # type: ignore[attr-defined]
    envelope = _parse_envelope(content)
    assert len(envelope.entity_refs) == 1
    ref = envelope.entity_refs[0]
    assert ref.ref_id == task["id"]
    assert ref.module == "tasks"
    assert ref.kind == TASK_KIND
    assert ref.title == "Deploy to prod"


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

    content, _ = await mod.mcp.call_tool("tasks_list", {})  # type: ignore[attr-defined]
    envelope = _parse_envelope(content)
    assert all(r.ref_id != task_id for r in envelope.entity_refs)


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


def test_task_entity_ref_shape() -> None:
    ref = task_entity_ref(_task(due="2026-06-20"))
    assert ref.ref_id == "t1"
    assert ref.module == "tasks"
    assert ref.kind == TASK_KIND
    assert ref.title == "Write report"
    assert ref.summary is not None
    assert "2026-06-20" in ref.summary


def test_task_entity_ref_summary_without_due() -> None:
    ref = task_entity_ref(_task())
    assert ref.summary == "No due date"


def test_task_hover_card_full() -> None:
    card = task_hover_card(_task(due="2026-06-20", notes="Q2 numbers"))
    assert card["title"] == "Write report"
    assert card["description"] == "Q2 numbers"
    labels = {d["label"]: d["value"] for d in card["details"]}
    assert labels["Due"] == "2026-06-20"
    assert labels["Status"] == "Open"
    assert card.get("href") is None


def test_task_hover_card_omits_due_when_absent_and_marks_completed() -> None:
    card = task_hover_card(_task(completed=True))
    labels = {d["label"]: d["value"] for d in card["details"]}
    assert "Due" not in labels
    assert labels["Status"] == "Completed"
    assert card["description"] == ""


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
