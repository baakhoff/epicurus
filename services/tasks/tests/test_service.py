"""Unit tests for the tasks module tool surface via the LocalTasksProvider."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import CONTRACT_VERSION, Collection, CollectionPrefs, CollectionRef
from epicurus_core.contracts import ToolEnvelope
from epicurus_tasks.db import TaskStore
from epicurus_tasks.local_provider import LocalTasksProvider
from epicurus_tasks.models import Task
from epicurus_tasks.router import TasksRouter
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
    tasks_accounts,
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
    assert manifest.version == "0.7.1"
    assert manifest.contract_version == CONTRACT_VERSION
    # Google Tasks API scope requested at connect (#241); identity scopes are the core default.
    assert manifest.oauth_scopes == {"google": ["https://www.googleapis.com/auth/tasks"]}
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
    # Account/collection model (ADR-0030): a single-active list picker, no provider dropdown.
    assert manifest.collections is not None
    assert manifest.collections.noun == "list"
    assert manifest.collections.multi is False
    assert manifest.collections.providers == ["google"]
    assert manifest.ui.config_schema is None


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
    card = task_hover_card(_task(status="done"))
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
    assert "Completed" in task_excerpt(_task(status="done"))


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


# ── Richer fields: priority, tags, status (issue #218) ───────────────────────


def test_hover_card_shows_priority_and_tags() -> None:
    card = task_hover_card(_task(priority="high", tags=["work", "q3"], due="2026-06-20"))
    labels = {d["label"]: d["value"] for d in card["details"]}
    assert labels["Priority"] == "High"
    assert labels["Tags"] == "work, q3"


def test_hover_card_omits_priority_when_absent() -> None:
    card = task_hover_card(_task())
    labels = {d["label"] for d in card["details"]}
    assert "Priority" not in labels
    assert "Tags" not in labels


def test_hover_card_shows_in_progress_status() -> None:
    card = task_hover_card(_task(status="in_progress"))
    labels = {d["label"]: d["value"] for d in card["details"]}
    assert labels["Status"] == "In Progress"


def test_task_summary_shows_in_progress() -> None:
    ref = task_entity_ref(_task(status="in_progress"))
    assert "In Progress" in ref.summary


def test_task_excerpt_includes_priority_and_tags() -> None:
    excerpt = task_excerpt(_task(priority="medium", tags=["work"]))
    assert "medium" in excerpt
    assert "work" in excerpt


async def test_add_task_with_rich_fields(module_fixture: object) -> None:
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add",
        {
            "title": "Rich task",
            "priority": "high",
            "tags": "work, urgent",
            "status": "in_progress",
        },
    )
    assert task["priority"] == "high"
    assert task["tags"] == ["work", "urgent"]
    assert task["status"] == "in_progress"
    assert not task["completed"]


async def test_update_task_rich_fields(module_fixture: object) -> None:
    mod = module_fixture
    _, task = await mod.mcp.call_tool("tasks_add", {"title": "Plain"})  # type: ignore[attr-defined]
    task_id = task["id"]

    _, updated = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_update",
        {"task_id": task_id, "priority": "medium", "tags": "home, weekend"},
    )
    assert updated["priority"] == "medium"
    assert updated["tags"] == ["home", "weekend"]


async def test_add_task_invalid_priority_raises(module_fixture: object) -> None:
    mod = module_fixture
    with pytest.raises(Exception, match="invalid priority"):
        await mod.mcp.call_tool("tasks_add", {"title": "Bad", "priority": "critical"})  # type: ignore[attr-defined]


async def test_add_task_invalid_status_raises(module_fixture: object) -> None:
    mod = module_fixture
    with pytest.raises(Exception, match="invalid status"):
        await mod.mcp.call_tool("tasks_add", {"title": "Bad", "status": "pending"})  # type: ignore[attr-defined]


# ── Connected accounts + router (ADR-0030) ────────────────────────────────────


class _FakeGoogleTasks:
    """Minimal in-memory Google-like tasks provider for accounts/router tests."""

    def __init__(self, *, connected: bool = True) -> None:
        self._connected = connected
        self.tasks: list[Task] = []
        self.last_list_id: str | None = None

    def provider_name(self) -> str:
        return "google"

    async def is_available(self, tenant_id: str) -> bool:
        return self._connected

    async def list_collections(self, tenant_id: str) -> list[Collection]:
        return [
            Collection(account="google", collection="@default", title="My Tasks"),
            Collection(account="google", collection="work", title="Work"),
        ]

    async def list_tasks(self, tenant_id: str, *, list_id: str | None = None) -> list[Task]:
        self.last_list_id = list_id
        return self.tasks

    async def add_task(
        self, tenant_id: str, title: str, *, list_id: str | None = None, **kw: Any
    ) -> Task:
        self.last_list_id = list_id
        created = Task(id="g1", title=title)
        self.tasks.append(created)
        return created

    async def complete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task:
        self.last_list_id = list_id
        return Task(id=task_id, title="done", status="done")

    async def update_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None, **kw: Any
    ) -> Task:
        self.last_list_id = list_id
        return Task(id=task_id, title="upd")

    async def get_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task | None:
        self.last_list_id = list_id
        return next((t for t in self.tasks if t.id == task_id), None)


class _StaticPrefs:
    def __init__(self, prefs: CollectionPrefs) -> None:
        self._prefs = prefs

    async def get_collections(self) -> CollectionPrefs:
        return self._prefs


async def test_tasks_accounts_lists_connected_google() -> None:
    view = await tasks_accounts({"google": _FakeGoogleTasks()}, tenant_id=TENANT)
    assert view.noun == "list"
    assert view.multi is False
    account = view.accounts[0]
    assert account.account == "google"
    assert account.connected is True
    assert [c.collection for c in account.collections] == ["@default", "work"]


async def test_tasks_accounts_omits_lists_when_disconnected() -> None:
    view = await tasks_accounts({"google": _FakeGoogleTasks(connected=False)}, tenant_id=TENANT)
    assert view.accounts[0].connected is False
    assert view.accounts[0].collections == []


async def _local_router(
    prefs: CollectionPrefs,
) -> tuple[TasksRouter, LocalTasksProvider, _FakeGoogleTasks]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = TaskStore(engine)
    await store.init()
    local = LocalTasksProvider(store)
    google = _FakeGoogleTasks()
    router = TasksRouter(local=local, external={"google": google}, prefs=_StaticPrefs(prefs))
    return router, local, google


async def test_router_uses_local_when_no_active() -> None:
    router, local, _google = await _local_router(CollectionPrefs())
    await local.add_task(TENANT, "Local task")
    tasks = await router.list_tasks(TENANT)
    assert [t.title for t in tasks] == ["Local task"]


async def test_router_routes_to_active_google_list() -> None:
    active = CollectionRef(account="google", collection="work")
    router, _local, google = await _local_router(CollectionPrefs(enabled=[active], active=active))
    google.tasks = [Task(id="g1", title="Google task")]
    tasks = await router.list_tasks(TENANT)
    assert [t.title for t in tasks] == ["Google task"]
    # The active collection id is passed through to the provider as list_id.
    assert google.last_list_id == "work"


async def test_router_add_routes_to_active_list() -> None:
    active = CollectionRef(account="google", collection="work")
    router, _local, google = await _local_router(CollectionPrefs(enabled=[active], active=active))
    created = await router.add_task(TENANT, "New")
    assert created.title == "New"
    assert google.last_list_id == "work"


async def test_router_falls_back_to_local_when_prefs_unavailable() -> None:
    class _BrokenPrefs:
        async def get_collections(self) -> CollectionPrefs:
            raise RuntimeError("core down")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = TaskStore(engine)
    await store.init()
    local = LocalTasksProvider(store)
    router = TasksRouter(local=local, external={}, prefs=_BrokenPrefs())
    await local.add_task(TENANT, "Survives")
    tasks = await router.list_tasks(TENANT)
    assert [t.title for t in tasks] == ["Survives"]
