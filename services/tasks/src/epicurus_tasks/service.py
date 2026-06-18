"""Tasks module — provider-agnostic MCP tool surface (ADR-0016).

Also serves the **Tasks** left-nav page: a core-rendered ``board`` archetype
(ADR-0018). The module supplies data only — :func:`build_tasks_board` groups open
tasks into due-date columns and attaches per-card actions that invoke the module's
own MCP tools through the core (complete / edit) plus a board-level add — and the
shell renders it. No markup ever leaves this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from epicurus_core import (
    Account,
    AccountsView,
    CollectionsSpec,
    EntityRef,
    EpicurusModule,
    HoverCard,
    HoverCardDetail,
    PageSpec,
    UiSection,
    tool_envelope,
)
from epicurus_tasks.google_provider import GoogleTasksError
from epicurus_tasks.models import VALID_PRIORITIES, VALID_STATUSES, Task
from epicurus_tasks.providers import TasksProvider

MODULE_NAME = "tasks"
TASKS_PAGE_ID = "board"
"""The id of the Tasks left-nav page; forms its nav route and data path."""

# The external providers the tasks module can connect (ADR-0030); ``local`` is the
# implicit default and is never listed. Maps the account id to its shell display label.
PROVIDER_LABELS = {"google": "Google"}

# The kind every task entity-reference and attachment carries (ADR-0019).
TASK_KIND = "task"

# Chat-attachment picker bound (ADR-0019): the composer lists open tasks to attach;
# the cap keeps the menu manageable for a long backlog.
_ATTACH_LIMIT = 50

_STATUS_LABEL: dict[str, str] = {
    "open": "Open",
    "in_progress": "In Progress",
    "done": "Completed",
}

_PRIORITY_TONE: dict[str, str] = {
    "high": "danger",
    "medium": "warn",
    "low": "dim",
}


def _parse_tags(raw: str | None) -> list[str]:
    """Split a comma-separated tags string into a cleaned list."""
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def build_module(provider: TasksProvider, *, tenant_id: str) -> EpicurusModule:
    """Register the provider-agnostic task tools and the Tasks page on the module.

    The tools are closed over *provider* and *tenant_id* at build time so the
    MCP tool signatures stay clean (no plumbing arguments leaked to the agent).
    """
    module = EpicurusModule(
        MODULE_NAME,
        version="0.6.0",
        description=(
            "Task management: list, add, edit, and complete tasks. Backed by a local"
            " store (no account needed) plus any Google task lists the operator connects."
        ),
        ui=UiSection(
            icon="check-square",
            summary=(
                "Manage your tasks in a built-in local list (no account needed) or in a"
                " **Google** task list you connect. Choose the active list below; the agent"
                " can list, add, edit, and complete tasks on it."
            ),
            # No config_schema: there is no provider dropdown any more (ADR-0030). Accounts
            # and the active list are managed in the connected-accounts section.
            status_url="/status",
        ),
        pages=[
            PageSpec(
                id=TASKS_PAGE_ID,
                title="Tasks",
                archetype="board",
                icon="check",
                nav_order=40,
            )
        ],
        resolver=True,
        attachable=True,
        # Account/collection model (ADR-0030): a silent local default plus connectable
        # Google task lists. Tasks is single-active (not multi): the board and tools act on
        # the active list. Serves GET /accounts.
        collections=CollectionsSpec(noun="list", multi=False, providers=["google"]),
    )

    @module.tool()
    async def tasks_list(list_id: str | None = None) -> str:
        """List open tasks from the active provider as entity-reference chips.

        Returns the tasks as entity-reference chips (ADR-0019): hover a chip for the
        task's hover-card, click it to open the task in the side panel. Each chip
        carries the task id, so you can refer to a task later without listing again.
        The accompanying text lists each task's title and due date.

        Args:
            list_id: Provider-specific list identifier.  Omit to use the
                provider's default list (e.g. ``"@default"`` for Google Tasks).

        Returns a tool envelope whose chips reference the matching open tasks.
        """
        try:
            tasks = await provider.list_tasks(tenant_id, list_id=list_id)
        except (GoogleTasksError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc
        if not tasks:
            return tool_envelope("No open tasks.", [])
        refs = [task_entity_ref(t) for t in tasks]
        lines = [f"- {t.title}" + (f" (due {t.due[:10]})" if t.due else "") for t in tasks]
        text = f"Found {len(tasks)} open task(s):\n" + "\n".join(lines)
        return tool_envelope(text, refs)

    @module.tool()
    async def tasks_add(
        title: str,
        notes: str | None = None,
        due: str | None = None,
        priority: str | None = None,
        tags: str | None = None,
        status: str = "open",
        list_id: str | None = None,
    ) -> Task:
        """Create a new task.

        Args:
            title: Task title (required).
            notes: Optional free-text notes or description.
            due: Optional due date as an ISO date string, e.g. ``"2025-01-15"``.
            priority: Optional priority level — ``"low"``, ``"medium"``, or ``"high"``.
                Google Tasks ignores this field.
            tags: Optional comma-separated labels, e.g. ``"work, urgent"``.
                Google Tasks ignores this field.
            status: Initial status — ``"open"`` (default), ``"in_progress"``, or
                ``"done"``.  Google Tasks maps ``"done"`` to completed;
                ``"in_progress"`` is local-only and reads back as ``"open"`` from Google.
            list_id: Target list identifier.  Omit for the default list.

        Returns the created :class:`Task`.
        """
        if priority is not None and priority not in VALID_PRIORITIES:
            raise RuntimeError(
                f"invalid priority {priority!r}; must be one of {sorted(VALID_PRIORITIES)}"
            )
        if status not in VALID_STATUSES:
            raise RuntimeError(
                f"invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}"
            )
        tag_list = _parse_tags(tags)
        try:
            return await provider.add_task(
                tenant_id,
                title,
                notes=notes,
                due=due,
                status=status,
                priority=priority,
                tags=tag_list,
                list_id=list_id,
            )
        except (GoogleTasksError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @module.tool()
    async def tasks_complete(task_id: str, list_id: str | None = None) -> Task:
        """Mark a task as complete.

        Args:
            task_id: The provider-specific task identifier (from ``tasks_list``).
            list_id: The list containing the task.  Omit for the default list.

        Returns the updated :class:`Task` with ``completed=True``.
        """
        try:
            return await provider.complete_task(tenant_id, task_id, list_id=list_id)
        except (GoogleTasksError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    @module.tool()
    async def tasks_update(
        task_id: str,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        priority: str | None = None,
        tags: str | None = None,
        status: str | None = None,
        list_id: str | None = None,
    ) -> Task:
        """Edit an existing task's title, notes, due date, priority, tags, or status.

        Only the fields you pass are changed; omitted fields keep their current
        value. To mark a task done use ``tasks_complete`` — this tool edits content.

        Args:
            task_id: The provider-specific task identifier (from ``tasks_list``).
            title: New title.  Omit to leave it unchanged.
            notes: New free-text notes.  Omit to leave them unchanged.
            due: New due date as an ISO date string, e.g. ``"2025-01-15"``.  Omit
                to leave it unchanged.
            priority: New priority (``"low"``/``"medium"``/``"high"``).  Omit to
                leave unchanged.  Google Tasks ignores this field.
            tags: New comma-separated tags, e.g. ``"work, urgent"``.  Omit to leave
                unchanged.  Google Tasks ignores this field.
            status: New status (``"open"``/``"in_progress"``/``"done"``).  Omit to
                leave unchanged.
            list_id: The list containing the task.  Omit for the default list.

        Returns the updated :class:`Task`.
        """
        if priority is not None and priority not in VALID_PRIORITIES:
            raise RuntimeError(
                f"invalid priority {priority!r}; must be one of {sorted(VALID_PRIORITIES)}"
            )
        if status is not None and status not in VALID_STATUSES:
            raise RuntimeError(
                f"invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}"
            )
        tag_list = _parse_tags(tags) if tags is not None else None
        try:
            return await provider.update_task(
                tenant_id,
                task_id,
                title=title,
                notes=notes,
                due=due,
                status=status,
                priority=priority,
                tags=tag_list,
                list_id=list_id,
            )
        except (GoogleTasksError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    return module


async def tasks_accounts(external: Mapping[str, TasksProvider], *, tenant_id: str) -> AccountsView:
    """The connected-accounts view backing ``GET /accounts`` (ADR-0030).

    One :class:`Account` per supported external provider, ``connected`` from the live
    OAuth check and ``collections`` (task lists) listed only when connected. ``local`` is
    the silent default and is never included. Tasks is single-active (``multi=False``).
    """
    accounts: list[Account] = []
    for account_id, provider in external.items():
        connected = await provider.is_available(tenant_id)
        collections = await provider.list_collections(tenant_id) if connected else []
        accounts.append(
            Account(
                account=account_id,
                provider=account_id,
                label=PROVIDER_LABELS.get(account_id, account_id.title()),
                connected=connected,
                collections=collections,
            )
        )
    return AccountsView(noun="list", multi=False, accounts=accounts)


# ── Tasks page: the `board` archetype data (ADR-0018) ───────────────────────────
#
# The module supplies data only; the core shell renders it. The board groups OPEN
# tasks into due-date columns and attaches per-card actions that the shell turns
# into buttons — each invokes one of this module's MCP tools through the core
# (validated against the manifest), so mutations never bypass the contract.

_BUCKET_ORDER = ("Overdue", "Today", "Upcoming", "No date")
_BUCKET_TONE = {"Overdue": "danger", "Today": "accent"}

# field_options teaches the shell's SchemaForm which values are valid for enum-like
# string fields.  The shell overlays these onto the tool's raw JSON schema so it
# can render a <select> instead of a free-text input (ADR-0018 board extension).
_TASK_FIELD_OPTIONS: dict[str, list[str]] = {
    "priority": ["low", "medium", "high"],
    "status": ["open", "in_progress", "done"],
}


def _bucket_for(task: Task, today: str) -> str:
    """The due-date column a task belongs in, relative to *today* (ISO date).

    ISO date strings sort lexicographically, so the comparison needs no parsing.
    """
    if not task.due:
        return "No date"
    due = task.due[:10]
    if due < today:
        return "Overdue"
    if due == today:
        return "Today"
    return "Upcoming"


def _task_card(task: Task, bucket: str) -> dict[str, Any]:
    """One board card: the task plus its complete / edit actions."""
    badges: list[dict[str, str]] = []
    if task.due:
        badges.append({"label": task.due[:10], "tone": _BUCKET_TONE.get(bucket, "dim")})
    if task.priority:
        badges.append({"label": task.priority.capitalize(), "tone": _PRIORITY_TONE[task.priority]})
    for tag in task.tags:
        badges.append({"label": tag, "tone": "accent"})

    return {
        "id": task.id,
        "title": task.title,
        "subtitle": task.notes or None,
        "badges": badges,
        "actions": [
            {
                "tool": "tasks_complete",
                "label": "Complete",
                "icon": "check",
                "args": {"task_id": task.id},
            },
            {
                "tool": "tasks_update",
                "label": "Edit",
                "icon": "pencil",
                "form": True,
                "fields": ["title", "notes", "due", "priority", "tags", "status"],
                "field_options": _TASK_FIELD_OPTIONS,
                "args": {"task_id": task.id},
                "form_values": {
                    "title": task.title,
                    "notes": task.notes or "",
                    "due": task.due or "",
                    "priority": task.priority or "",
                    "tags": ", ".join(task.tags),
                    "status": task.status,
                },
            },
        ],
    }


def build_tasks_board(tasks: list[Task], *, today: str) -> dict[str, Any]:
    """Build the ``board`` archetype payload for the Tasks page (ADR-0018).

    Pure and deterministic given *today* (an ISO date, e.g. ``"2026-06-14"``) so the
    bucketing is unit-testable without a clock. Empty columns are dropped; a
    board-level **Add task** action is always offered.
    """
    grouped: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in _BUCKET_ORDER}
    for task in tasks:
        bucket = _bucket_for(task, today)
        grouped[bucket].append(_task_card(task, bucket))

    columns = [
        {
            "id": bucket.lower().replace(" ", "-"),
            "title": bucket,
            "cards": grouped[bucket],
        }
        for bucket in _BUCKET_ORDER
        if grouped[bucket]
    ]
    return {
        "title": "Tasks",
        "columns": columns,
        "actions": [
            {
                "tool": "tasks_add",
                "label": "Add task",
                "intent": "primary",
                "icon": "plus",
                "form": True,
                "fields": ["title", "notes", "due", "priority", "tags"],
                "field_options": _TASK_FIELD_OPTIONS,
            }
        ],
    }


# ── Entity references, hover-cards & attachments (ADR-0019) ───────────────────
#
# `tasks_list` returns its tasks as entity-reference chips; the module resolves a
# referenced task to a core hover-card; and it is a chat-attachment source. These
# helpers (provider-agnostic and app-free) back those surfaces so they are unit-
# testable without a running app; a task is fetched by id via the active provider's
# `get_task`, so they behave identically against the local and Google backends.


class TaskNotFound(Exception):
    """Raised when a task id does not resolve for the active provider/tenant."""


def _task_summary(task: Task) -> str:
    """A compact one-line summary for a task chip (due date, then status)."""
    parts = [f"Due {task.due[:10]}" if task.due else "No due date"]
    status_label = _STATUS_LABEL.get(task.status, task.status)
    if task.status != "open":
        parts.append(status_label)
    return " · ".join(parts)


def task_entity_ref(task: Task) -> EntityRef:
    """The chip an agent turn carries for a listed task (ADR-0019)."""
    return EntityRef(
        ref_id=task.id,
        module=MODULE_NAME,
        kind=TASK_KIND,
        title=task.title,
        summary=_task_summary(task),
    )


def task_hover_card(task: Task) -> dict[str, Any]:
    """The core hover-card / entity-detail envelope for a task (ADR-0019).

    Core-owned, uniform shape: the module supplies the data, the shell renders the
    inline hover-card and the panel's entity-detail view from it.
    """
    details: list[HoverCardDetail] = []
    if task.due:
        details.append(HoverCardDetail(label="Due", value=task.due[:10]))
    details.append(
        HoverCardDetail(label="Status", value=_STATUS_LABEL.get(task.status, task.status))
    )
    if task.priority:
        details.append(HoverCardDetail(label="Priority", value=task.priority.capitalize()))
    if task.tags:
        details.append(HoverCardDetail(label="Tags", value=", ".join(task.tags)))
    return HoverCard(
        title=task.title,
        description=task.notes or "",
        details=details,
    ).model_dump()


def task_excerpt(task: Task) -> str:
    """A short plain-text rendering of a task for the agent's turn context."""
    lines = [task.title]
    if task.due:
        lines.append(f"Due {task.due[:10]}")
    lines.append(_STATUS_LABEL.get(task.status, task.status))
    if task.priority:
        lines.append(f"Priority: {task.priority}")
    if task.tags:
        lines.append(f"Tags: {', '.join(task.tags)}")
    if task.notes:
        lines.extend(["", task.notes])
    return "\n".join(lines)


def task_attachment_item(task: Task) -> dict[str, str]:
    """One picker row the composer lists for the attachment source (ADR-0019)."""
    return {"ref_id": task.id, "kind": TASK_KIND, "title": task.title}


def task_attachment(task: Task) -> dict[str, str]:
    """The resolve payload the agent injects when an attached task is expanded."""
    return {"title": task.title, "excerpt": task_excerpt(task)}


async def fetch_task(provider: TasksProvider, *, tenant_id: str, ref_id: str) -> Task:
    """Fetch one task by id, raising :class:`TaskNotFound` when it does not exist."""
    task = await provider.get_task(tenant_id, ref_id)
    if task is None:
        raise TaskNotFound(ref_id)
    return task


async def tasks_attachments(
    provider: TasksProvider,
    *,
    tenant_id: str,
    limit: int = _ATTACH_LIMIT,
) -> list[dict[str, str]]:
    """Picker for the chat-attachment composer (ADR-0019): open tasks as items.

    Returns up to *limit* open tasks from the active provider's default list as
    ``{ref_id, kind, title}`` rows.
    """
    tasks = await provider.list_tasks(tenant_id)
    return [task_attachment_item(t) for t in tasks[:limit]]
