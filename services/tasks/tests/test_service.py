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
    assert manifest.version == "0.15.0"
    assert manifest.contract_version == CONTRACT_VERSION
    # Google Tasks API scope requested at connect (#241); identity scopes are the core default.
    assert manifest.oauth_scopes == {"google": ["https://www.googleapis.com/auth/tasks"]}
    tool_names = {t.name for t in manifest.tools}
    assert tool_names == {
        "tasks_list",
        "tasks_lists",
        "tasks_create_list",
        "tasks_add",
        "tasks_complete",
        "tasks_update",
        "tasks_delete",
    }
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
    # Account/collection model (ADR-0030/0036): multi — each enabled list is a category.
    assert manifest.collections is not None
    assert manifest.collections.noun == "list"
    assert manifest.collections.multi is True
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


async def test_tasks_delete(module_fixture: object) -> None:
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "Throwaway"}
    )
    task_id = task["id"]

    content, _ = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_delete", {"task_id": task_id}
    )
    assert "deleted" in content[0].text.lower()

    # The task is gone — listing returns nothing.
    content, _ = await mod.mcp.call_tool("tasks_list", {})  # type: ignore[attr-defined]
    envelope = _parse_envelope(content)
    assert envelope.entity_refs == []


async def test_tasks_delete_unknown_id_is_a_noop(module_fixture: object) -> None:
    mod = module_fixture
    # The local store deletes by id without error, so deleting a missing task is harmless.
    content, _ = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_delete", {"task_id": "does-not-exist"}
    )
    assert "deleted" in content[0].text.lower()


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


# ── Clear sentinel + no-op rejection (#475: agent phantom updates) ────────────


async def test_tasks_update_rejects_field_less_call(module_fixture: object) -> None:
    """A tasks_update with nothing to change is a loud error, not a silent success."""
    mod = module_fixture
    _, task = await mod.mcp.call_tool("tasks_add", {"title": "Untouched"})  # type: ignore[attr-defined]

    with pytest.raises(Exception, match="nothing to change"):
        await mod.mcp.call_tool(  # type: ignore[attr-defined]
            "tasks_update", {"task_id": task["id"]}
        )


async def test_tasks_update_with_only_to_list_id_is_not_rejected(module_fixture: object) -> None:
    """to_list_id alone is a legitimate move, not a no-op — the local provider ignores the
    move target (single flat list) but the call must not raise "nothing to change"."""
    mod = module_fixture
    _, task = await mod.mcp.call_tool("tasks_add", {"title": "Movable"})  # type: ignore[attr-defined]

    _, updated = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_update", {"task_id": task["id"], "to_list_id": "somewhere"}
    )
    assert updated["title"] == "Movable"


async def test_tasks_update_clears_due_with_empty_string(module_fixture: object) -> None:
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "Has a date", "due": "2026-01-01"}
    )

    _, updated = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_update", {"task_id": task["id"], "due": ""}
    )
    assert updated["due"] is None


async def test_tasks_update_clears_notes_with_empty_string(module_fixture: object) -> None:
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "Has notes", "notes": "some notes"}
    )

    _, updated = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_update", {"task_id": task["id"], "notes": ""}
    )
    assert updated["notes"] is None


# ── Recurrence at the tool boundary (#471, ADR-0082) ──────────────────────────


async def test_tasks_add_persists_repeat(module_fixture: object) -> None:
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "Water plants", "due": "2026-07-06", "repeat": "FREQ=WEEKLY"}
    )
    assert task["repeat"] == "FREQ=WEEKLY"


async def test_tasks_add_repeat_requires_a_due_date(module_fixture: object) -> None:
    mod = module_fixture
    with pytest.raises(Exception, match="due date"):
        await mod.mcp.call_tool(  # type: ignore[attr-defined]
            "tasks_add", {"title": "No anchor", "repeat": "FREQ=DAILY"}
        )


async def test_tasks_add_rejects_invalid_repeat(module_fixture: object) -> None:
    mod = module_fixture
    with pytest.raises(Exception, match="invalid recurrence rule"):
        await mod.mcp.call_tool(  # type: ignore[attr-defined]
            "tasks_add", {"title": "Bad rule", "due": "2026-07-06", "repeat": "NONSENSE"}
        )


async def test_tasks_update_only_repeat_is_not_rejected(module_fixture: object) -> None:
    """`repeat` counts as a mutable field, so a repeat-only update isn't a no-op error (#475)."""
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "Make recurring", "due": "2026-07-06"}
    )
    _, updated = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_update", {"task_id": task["id"], "repeat": "FREQ=WEEKLY"}
    )
    assert updated["repeat"] == "FREQ=WEEKLY"


async def test_tasks_update_clears_repeat_with_empty_string(module_fixture: object) -> None:
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "Recurring", "due": "2026-07-06", "repeat": "FREQ=WEEKLY"}
    )
    _, updated = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_update", {"task_id": task["id"], "repeat": ""}
    )
    assert updated["repeat"] is None


async def test_tasks_update_rejects_repeat_on_a_task_with_no_due_date(
    module_fixture: object,
) -> None:
    """Setting `repeat` on a task with no due — and none supplied here — can't anchor the
    rule, so it would silently never materialize (#515). Rejects like tasks_add does."""
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "No date yet"}
    )
    with pytest.raises(Exception, match="due date"):
        await mod.mcp.call_tool(  # type: ignore[attr-defined]
            "tasks_update", {"task_id": task["id"], "repeat": "FREQ=WEEKLY"}
        )


async def test_tasks_update_accepts_repeat_when_due_supplied_in_the_same_call(
    module_fixture: object,
) -> None:
    mod = module_fixture
    _, task = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_add", {"title": "No date yet"}
    )
    _, updated = await mod.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_update", {"task_id": task["id"], "due": "2026-08-01", "repeat": "FREQ=WEEKLY"}
    )
    assert updated["repeat"] == "FREQ=WEEKLY"
    assert updated["due"] == "2026-08-01"


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


def test_hover_card_shows_repeat() -> None:
    card = task_hover_card(_task(due="2026-07-06", repeat="FREQ=WEEKLY"))
    labels = {d["label"]: d["value"] for d in card["details"]}
    assert labels["Repeat"] == "Weekly"


def test_hover_card_omits_repeat_for_one_off() -> None:
    card = task_hover_card(_task())
    assert "Repeat" not in {d["label"] for d in card["details"]}


def test_task_excerpt_includes_repeat() -> None:
    assert "Repeats weekly" in task_excerpt(_task(due="2026-07-06", repeat="FREQ=WEEKLY"))


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
    """Minimal in-memory Google-like tasks provider for accounts/router tests.

    ``tasks`` is the default bucket; ``tasks_by_list`` overrides it per list id so the
    aggregate path can be asserted. ``fail_lists`` / ``fail_titles`` simulate a failing
    ``list_tasks`` for one list and a failing ``list_collections`` (title lookup).
    """

    def __init__(self, *, connected: bool = True) -> None:
        self._connected = connected
        self.tasks: list[Task] = []
        self.tasks_by_list: dict[str | None, list[Task]] = {}
        self.last_list_id: str | None = None
        self.list_ids_seen: list[str | None] = []
        self.fail_lists: set[str | None] = set()
        self.fail_titles = False
        self.add_targets: list[str | None] = []  # list ids add_task was routed to
        self.deleted: list[tuple[str | None, str]] = []  # (list_id, task_id) deletes

    def provider_name(self) -> str:
        return "google"

    async def is_available(self, tenant_id: str) -> bool:
        return self._connected

    async def list_collections(self, tenant_id: str) -> list[Collection]:
        if self.fail_titles:
            raise RuntimeError("discovery failed")
        return [
            Collection(account="google", collection="@default", title="My Tasks"),
            Collection(account="google", collection="work", title="Work"),
        ]

    async def list_tasks(
        self, tenant_id: str, *, list_id: str | None = None, scope: str = "open"
    ) -> list[Task]:
        self.last_list_id = list_id
        self.list_ids_seen.append(list_id)
        if list_id in self.fail_lists:
            raise RuntimeError("list read failed")
        return self.tasks_by_list.get(list_id, self.tasks)

    async def add_task(
        self, tenant_id: str, title: str, *, list_id: str | None = None, **kw: Any
    ) -> Task:
        self.last_list_id = list_id
        self.add_targets.append(list_id)
        created = Task(id="g1", title=title)
        self.tasks.append(created)
        return created

    async def complete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task:
        self.last_list_id = list_id
        return Task(id=task_id, title="done", status="done")

    async def delete_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> None:
        self.deleted.append((list_id, task_id))
        self.tasks = [t for t in self.tasks if t.id != task_id]

    async def update_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None, **kw: Any
    ) -> Task:
        self.last_list_id = list_id
        return Task(id=task_id, title="upd")

    async def get_task(
        self, tenant_id: str, task_id: str, *, list_id: str | None = None
    ) -> Task | None:
        self.last_list_id = list_id
        self.list_ids_seen.append(list_id)
        if list_id in self.fail_lists:
            raise RuntimeError("list read failed")
        # Scoped the same way as list_tasks: a list not overridden in tasks_by_list
        # falls back to the flat `tasks` bucket (so single-list tests are unaffected).
        bucket = self.tasks_by_list.get(list_id, self.tasks)
        return next((t for t in bucket if t.id == task_id), None)

    async def create_list(self, tenant_id: str, title: str) -> Collection:
        return Collection(account="google", collection="new-list-id", title=title)


class _StaticPrefs:
    def __init__(self, prefs: CollectionPrefs) -> None:
        self._prefs = prefs

    async def get_collections(self) -> CollectionPrefs:
        return self._prefs


async def test_tasks_accounts_lists_connected_google() -> None:
    view = await tasks_accounts({"google": _FakeGoogleTasks()}, tenant_id=TENANT)
    assert view.noun == "list"
    assert view.multi is True
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


async def test_router_uses_local_when_nothing_enabled() -> None:
    router, local, _google = await _local_router(CollectionPrefs())
    await local.add_task(TENANT, "Local task")
    tasks = await router.list_tasks(TENANT)
    assert [t.title for t in tasks] == ["Local task"]
    # The local default reads back as the "Personal" category (ADR-0036).
    assert tasks[0].list_id is None
    assert tasks[0].list_title == "Personal"


async def test_router_aggregates_enabled_lists() -> None:
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks_by_list = {
        "@default": [Task(id="d1", title="Personal thing")],
        "work": [Task(id="w1", title="Work thing")],
    }
    tasks = await router.list_tasks(TENANT)
    assert {t.title for t in tasks} == {"Personal thing", "Work thing"}
    assert set(google.list_ids_seen) == {"@default", "work"}  # both lists read
    # each task is stamped with the list (category) it came from
    by_title = {t.title: t for t in tasks}
    assert (by_title["Work thing"].list_id, by_title["Work thing"].list_title) == ("work", "Work")
    assert by_title["Personal thing"].list_title == "My Tasks"


async def test_router_skips_a_failing_list_on_aggregate() -> None:
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks_by_list = {"work": [Task(id="w1", title="Work thing")]}
    google.fail_lists = {"@default"}  # one list errors → skipped, not fatal (#209)
    tasks = await router.list_tasks(TENANT)
    assert [t.title for t in tasks] == ["Work thing"]


async def test_router_list_tasks_single_list_id() -> None:
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks_by_list = {"work": [Task(id="w1", title="Just work")]}
    tasks = await router.list_tasks(TENANT, list_id="work")
    assert [t.title for t in tasks] == ["Just work"]
    assert google.list_ids_seen == ["work"]  # only the named list is read
    assert tasks[0].list_title == "Work"


async def test_router_title_lookup_failure_falls_back_to_ids() -> None:
    refs = [CollectionRef(account="google", collection="work")]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks_by_list = {"work": [Task(id="w1", title="Work thing")]}
    google.fail_titles = True  # discovery (titles) unavailable → label falls back to the id
    tasks = await router.list_tasks(TENANT)
    assert [t.title for t in tasks] == ["Work thing"]
    assert tasks[0].list_title == "work"


async def test_router_add_routes_to_named_list() -> None:
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    await router.add_task(TENANT, "New", list_id="work")
    assert google.last_list_id == "work"


async def test_router_add_defaults_to_active_then_first_enabled() -> None:
    work = CollectionRef(account="google", collection="work")
    default = CollectionRef(account="google", collection="@default")
    # active set → default target is the active list
    router, _local, google = await _local_router(
        CollectionPrefs(enabled=[default, work], active=work)
    )
    await router.add_task(TENANT, "A")
    assert google.last_list_id == "work"
    # no active → default target is the first enabled list
    router2, _l2, google2 = await _local_router(CollectionPrefs(enabled=[default, work]))
    await router2.add_task(TENANT, "B")
    assert google2.last_list_id == "@default"


async def test_router_complete_and_update_route_by_list_id() -> None:
    refs = [CollectionRef(account="google", collection="work")]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    await router.complete_task(TENANT, "g1", list_id="work")
    assert google.last_list_id == "work"
    await router.update_task(TENANT, "g1", title="x", list_id="work")
    assert google.last_list_id == "work"


async def test_router_get_task_searches_enabled_then_local() -> None:
    refs = [CollectionRef(account="google", collection="work")]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks = [Task(id="g1", title="On the work list")]
    found = await router.get_task(TENANT, "g1")  # no list_id → searches enabled, then local
    assert found is not None
    assert found.title == "On the work list"


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


# ── Moving a task between lists (to_list_id, ADR-0038) ─────────────────────────


async def test_router_update_moves_task_between_lists() -> None:
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks = [Task(id="d1", title="Move me", notes="n")]
    moved = await router.update_task(TENANT, "d1", list_id="@default", to_list_id="work")
    # Recreated in the target list and deleted from the source (Google has no move API).
    assert "work" in google.add_targets
    assert ("@default", "d1") in google.deleted
    # The returned task is stamped with its new list (category).
    assert moved.list_id == "work"
    assert moved.list_title == "Work"


async def test_router_update_in_place_when_target_equals_source() -> None:
    refs = [CollectionRef(account="google", collection="work")]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks = [Task(id="w1", title="Stay")]
    await router.update_task(TENANT, "w1", list_id="work", to_list_id="work")
    # Same list → a normal in-place edit, never a recreate/delete.
    assert google.deleted == []
    assert google.add_targets == []


async def test_router_update_move_missing_task_raises() -> None:
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, _google = await _local_router(CollectionPrefs(enabled=refs))
    with pytest.raises(ValueError):
        await router.update_task(TENANT, "nope", list_id="@default", to_list_id="work")


async def test_tasks_update_tool_moves_via_router() -> None:
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks = [Task(id="d1", title="Move me")]
    module = build_module(router, tenant_id=TENANT)
    await module.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_update", {"task_id": "d1", "list_id": "@default", "to_list_id": "work"}
    )
    assert ("@default", "d1") in google.deleted
    assert "work" in google.add_targets


async def test_local_provider_delete_task() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = TaskStore(engine)
    await store.init()
    local = LocalTasksProvider(store)
    created = await local.add_task(TENANT, "Bye")
    await local.delete_task(TENANT, created.id)
    assert await local.get_task(TENANT, created.id) is None


# ── tasks_lists discovery tool (so the agent can pick a list, #257) ────────────


async def test_tasks_lists_tool_reports_categories() -> None:
    async def cats() -> list[tuple[str, str]]:
        return [("@default", "My Tasks"), ("work", "Work")]

    module = build_module(_FakeGoogleTasks(), tenant_id=TENANT, categories=cats)
    content, _ = await module.mcp.call_tool("tasks_lists", {})  # type: ignore[attr-defined]
    text = content[0].text
    assert "My Tasks — id: @default" in text
    assert "Work — id: work" in text


async def test_tasks_lists_tool_reports_default_only_without_categories() -> None:
    module = build_module(_FakeGoogleTasks(), tenant_id=TENANT)  # no discovery hook
    content, _ = await module.mcp.call_tool("tasks_lists", {})  # type: ignore[attr-defined]
    assert "default task list" in content[0].text.lower()


# ── Cross-list resolution when list_id is omitted (#475) ──────────────────────
#
# complete_task / update_task / delete_task must find the task even when it lives in a
# non-default enabled list, instead of assuming the default write target and 404ing there
# (the incident: the agent tried to edit a task that lived outside "@default").


async def test_router_update_without_list_id_resolves_across_lists() -> None:
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    # The task lives only in "work" — not the default write target ("@default").
    google.tasks_by_list = {"@default": [], "work": [Task(id="w1", title="Hiding in work")]}
    await router.update_task(TENANT, "w1", title="Found me")
    # The default list was checked and missed before "work" was tried and matched.
    assert google.list_ids_seen == ["@default", "work"]
    assert google.last_list_id == "work"


async def test_router_complete_without_list_id_resolves_across_lists() -> None:
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks_by_list = {"@default": [], "work": [Task(id="w1", title="Hiding in work")]}
    await router.complete_task(TENANT, "w1")
    assert google.list_ids_seen == ["@default", "work"]
    assert google.last_list_id == "work"


async def test_router_delete_without_list_id_resolves_across_lists() -> None:
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks_by_list = {"@default": [], "work": [Task(id="w1", title="Hiding in work")]}
    await router.delete_task(TENANT, "w1")
    assert google.list_ids_seen == ["@default", "work"]
    assert ("work", "w1") in google.deleted


async def test_router_update_falls_back_to_default_when_task_not_found_anywhere() -> None:
    """An id that doesn't exist in any enabled/local list still routes to the default
    write target — preserving the prior behavior for a genuinely bad id (the provider,
    not the router, is what raises/404s for it)."""
    refs = [CollectionRef(account="google", collection="work")]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks = []  # not found in "work", nor in local
    await router.update_task(TENANT, "ghost", title="x")
    assert google.last_list_id == "work"


async def test_router_locate_task_skips_a_failing_list() -> None:
    """A source that errors during the search is skipped, not fatal (#209), and the search
    continues to the next candidate."""
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.fail_lists = {"@default"}
    google.tasks_by_list = {"work": [Task(id="w1", title="Hiding in work")]}
    await router.update_task(TENANT, "w1", title="Found me")
    assert google.list_ids_seen == ["@default", "work"]
    assert google.last_list_id == "work"


async def test_tasks_complete_tool_resolves_across_lists_without_list_id() -> None:
    """The MCP tool surface benefits from the same resolution (agent never passes list_id)."""
    refs = [
        CollectionRef(account="google", collection="@default"),
        CollectionRef(account="google", collection="work"),
    ]
    router, _local, google = await _local_router(CollectionPrefs(enabled=refs))
    google.tasks_by_list = {"@default": [], "work": [Task(id="w1", title="Hiding in work")]}
    module = build_module(router, tenant_id=TENANT)
    await module.mcp.call_tool("tasks_complete", {"task_id": "w1"})  # type: ignore[attr-defined]
    assert google.last_list_id == "work"


# ── create_list: Google-only, #474 ─────────────────────────────────────────────


async def test_router_create_list_routes_to_sole_external_provider() -> None:
    router, _local, _google = await _local_router(CollectionPrefs())
    created = await router.create_list(TENANT, "Groceries")
    assert created.account == "google"
    assert created.collection == "new-list-id"
    assert created.title == "Groceries"


async def test_router_create_list_raises_with_no_external_provider() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = TaskStore(engine)
    await store.init()
    local = LocalTasksProvider(store)
    router = TasksRouter(local=local, external={}, prefs=_StaticPrefs(CollectionPrefs()))
    with pytest.raises(ValueError, match="no external account"):
        await router.create_list(TENANT, "Groceries")


async def test_router_create_list_raises_when_ambiguous() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = TaskStore(engine)
    await store.init()
    local = LocalTasksProvider(store)
    external = {"google": _FakeGoogleTasks(), "google2": _FakeGoogleTasks()}
    router = TasksRouter(local=local, external=external, prefs=_StaticPrefs(CollectionPrefs()))
    with pytest.raises(ValueError, match="more than one"):
        await router.create_list(TENANT, "Groceries")


async def _empty_store() -> TaskStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = TaskStore(engine)
    await store.init()
    return store


async def test_tasks_create_list_tool_returns_the_new_collection() -> None:
    router, _local, _google = await _local_router(CollectionPrefs())
    module = build_module(router, tenant_id=TENANT)
    _, result = await module.mcp.call_tool(  # type: ignore[attr-defined]
        "tasks_create_list", {"title": "Groceries"}
    )
    assert result["account"] == "google"
    assert result["collection"] == "new-list-id"
    assert result["title"] == "Groceries"


async def test_tasks_create_list_tool_raises_with_no_external_provider() -> None:
    module = build_module(LocalTasksProvider(await _empty_store()), tenant_id=TENANT)
    with pytest.raises(Exception, match="connect Google"):
        await module.mcp.call_tool(  # type: ignore[attr-defined]
            "tasks_create_list", {"title": "Groceries"}
        )
