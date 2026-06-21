"""Tasks module — provider-agnostic MCP tool surface (ADR-0016).

Also serves the **Tasks** left-nav page: a core-rendered ``board`` archetype
(ADR-0018). The module supplies data only — :func:`build_tasks_board` groups open
tasks into due-date columns and attaches per-card actions that invoke the module's
own MCP tools through the core (complete / edit) plus a board-level add — and the
shell renders it. No markup ever leaves this module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from epicurus_core import (
    LOCAL_ACCOUNT,
    Account,
    AccountsView,
    CollectionPrefs,
    CollectionsSpec,
    EntityRef,
    EpicurusModule,
    HoverCard,
    HoverCardDetail,
    PageSpec,
    UiSection,
    get_logger,
    tool_envelope,
)
from epicurus_tasks.google_provider import GoogleTasksError
from epicurus_tasks.models import VALID_PRIORITIES, VALID_STATUSES, Task
from epicurus_tasks.providers import TasksProvider

log = get_logger("epicurus_tasks.service")

MODULE_NAME = "tasks"
TASKS_PAGE_ID = "board"

# An async hook returning the operator's enabled writable lists as ``(id, title)`` pairs.
# Backs the ``tasks_lists`` tool so the chat agent can discover lists and pick one when
# adding/moving (the web pickers get the same data via the page). ``None`` in unit tests.
ListCategories = Callable[[], Awaitable[list[tuple[str, str]]]]
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


def build_module(
    provider: TasksProvider,
    *,
    tenant_id: str,
    categories: ListCategories | None = None,
) -> EpicurusModule:
    """Register the provider-agnostic task tools and the Tasks page on the module.

    The tools are closed over *provider* and *tenant_id* at build time so the
    MCP tool signatures stay clean (no plumbing arguments leaked to the agent).

    Args:
        provider: The tasks backend (the ``TasksRouter`` in the running service).
        tenant_id: Default tenant for all tool calls.
        categories: Optional async hook returning the operator's enabled writable lists as
            ``(id, title)`` pairs; backs the ``tasks_lists`` discovery tool. ``None`` (unit
            tests / no external account) makes ``tasks_lists`` report only the default list.
    """
    module = EpicurusModule(
        MODULE_NAME,
        version="0.9.0",
        description=(
            "Task management: list, add, edit, and complete tasks. Backed by a local"
            " store (no account needed) plus any Google task lists the operator connects."
        ),
        ui=UiSection(
            icon="check-square",
            summary=(
                "Manage your tasks in a built-in local list (no account needed) or in the"
                " **Google** task lists you connect. Each enabled list is a category: the"
                " board shows tasks from all of them, and you pick the list when adding."
            ),
            # No config_schema: there is no provider dropdown any more (ADR-0030). Accounts
            # and the enabled lists are managed in the connected-accounts section.
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
        # Account/collection model (ADR-0030/0036): a silent local default plus connectable
        # Google task lists. Tasks is ``multi`` — each enabled list is a category: the board
        # aggregates open tasks across all enabled lists and the Add form picks the target
        # list. Serves GET /accounts.
        collections=CollectionsSpec(noun="list", multi=True, providers=["google"]),
        # The Google API scope the shell requests when connecting an account (#241); the
        # core adds the default identity scopes. Without this, the Google Tasks API 403s.
        oauth_scopes={"google": ["https://www.googleapis.com/auth/tasks"]},
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
    async def tasks_lists() -> str:
        """List the task lists (categories) available to add to or move tasks between.

        Call this before adding when the user hasn't named a list: if more than one list
        is shown, ask which one and pass its id as ``list_id`` to ``tasks_add`` (or as
        ``to_list_id`` to ``tasks_update`` to move a task). Omitting the id uses the
        default list.

        Returns the available lists as ``- <title> — id: <id>`` lines.
        """
        options = await categories() if categories is not None else []
        if not options:
            return "Only the default task list is available — add tasks without a list_id."
        lines = [f"- {title} — id: {list_id}" for list_id, title in options]
        return "Available task lists:\n" + "\n".join(lines)

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

        If more than one task list exists and the user hasn't said which to use, call
        ``tasks_lists`` first and ask which list, then pass its id as ``list_id``.

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
        to_list_id: str | None = None,
    ) -> Task:
        """Edit an existing task's title, notes, due date, priority, tags, or status.

        Only the fields you pass are changed; omitted fields keep their current
        value. To mark a task done use ``tasks_complete`` — this tool edits content.

        To **move** the task to another list, pass ``to_list_id`` (a list id from
        ``tasks_lists``). On Google Tasks a move recreates the task in the target list —
        it gets a new id, and subtasks/ordering aren't carried over.

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
            list_id: The list the task currently lives in.  Omit for the default list.
            to_list_id: Move the task to this list.  Omit to leave it where it is; when
                equal to its current list it's a no-op move (a normal edit).

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
                to_list_id=to_list_id,
            )
        except (GoogleTasksError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    return module


async def tasks_accounts(external: Mapping[str, TasksProvider], *, tenant_id: str) -> AccountsView:
    """The connected-accounts view backing ``GET /accounts`` (ADR-0030).

    One :class:`Account` per supported external provider, ``connected`` from the live
    OAuth check and ``collections`` (task lists) listed only when connected. ``local`` is
    the silent default and is never included. Tasks is ``multi`` — each enabled list is a
    category (ADR-0036).
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
    return AccountsView(noun="list", multi=True, accounts=accounts)


async def enabled_write_lists(
    external: Mapping[str, TasksProvider],
    prefs: CollectionPrefs,
    *,
    tenant_id: str,
) -> tuple[list[tuple[str, str]], str | None]:
    """The writable enabled external lists ``(id, title)`` + the default new-task target.

    Backs the board's Add-task list (category) picker (ADR-0036). Titles come from each
    external account's discovery; a lookup failure degrades to the list id and never fails
    the board. The default target is the active list (when it is a writable enabled list),
    else the first enabled list, else ``None`` (the local store, when nothing is enabled).
    """
    enabled = [ref for ref in prefs.enabled if ref.account != LOCAL_ACCOUNT]
    if not enabled:
        return [], None
    titles: dict[tuple[str, str], tuple[str, bool]] = {}
    for account in {ref.account for ref in enabled}:
        provider = external.get(account)
        if provider is None:
            continue
        try:
            for col in await provider.list_collections(tenant_id):
                titles[(col.account, col.collection)] = (col.title, col.writable)
        except Exception as exc:
            log.warning(
                "task-list discovery failed for picker; using ids",
                account=account,
                error=str(exc),
            )
    lists: list[tuple[str, str]] = []
    for ref in enabled:
        title, writable = titles.get((ref.account, ref.collection), (ref.collection, True))
        if writable:
            lists.append((ref.collection, title))
    valid = {list_id for list_id, _ in lists}
    active = prefs.active
    default = (
        active.collection if (active is not None and active.account != LOCAL_ACCOUNT) else None
    )
    if default not in valid:
        default = lists[0][0] if lists else None
    return lists, default


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


def _task_card(
    task: Task, bucket: str, *, move_lists: list[tuple[str, str]] | None = None
) -> dict[str, Any]:
    """One board card: the task plus its complete / edit actions.

    Each card carries ``list_id`` (the list the task belongs to) in its action args so a
    Complete / Edit routes back to the owning list when the board aggregates several lists
    (ADR-0036); a per-card category tag names that list. When *move_lists* is given (more
    than one writable list exists) the Edit form gains a **List** picker, prefilled to the
    task's current list, that moves the task when changed (ADR-0038).
    """
    badges: list[dict[str, str]] = []
    if task.due:
        badges.append({"label": task.due[:10], "tone": _BUCKET_TONE.get(bucket, "dim")})
    if task.priority:
        badges.append({"label": task.priority.capitalize(), "tone": _PRIORITY_TONE[task.priority]})
    for tag in task.tags:
        badges.append({"label": tag, "tone": "accent"})
    if task.list_title:  # the list (category) the task came from
        badges.append({"label": task.list_title, "tone": "dim"})

    args = {"task_id": task.id, "list_id": task.list_id}
    edit_action: dict[str, Any] = {
        "tool": "tasks_update",
        "label": "Edit",
        "icon": "pencil",
        "form": True,
        "fields": ["title", "notes", "due", "priority", "tags", "status"],
        "field_options": _TASK_FIELD_OPTIONS,
        "args": args,
        "form_values": {
            "title": task.title,
            "notes": task.notes or "",
            "due": task.due or "",
            "priority": task.priority or "",
            "tags": ", ".join(task.tags),
            "status": task.status,
        },
    }
    if move_lists:
        # The List picker is the move target (`to_list_id`); the source stays in `args`.
        edit_action["fields"] = [
            "title",
            "to_list_id",
            "notes",
            "due",
            "priority",
            "tags",
            "status",
        ]
        edit_action["field_choices"] = {
            "to_list_id": [{"value": list_id, "label": title} for list_id, title in move_lists],
        }
        edit_action["form_values"]["to_list_id"] = task.list_id or ""
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
                "args": args,
            },
            edit_action,
        ],
    }


def build_tasks_board(
    tasks: list[Task],
    *,
    today: str,
    lists: list[tuple[str, str]] | None = None,
    default_list_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``board`` archetype payload for the Tasks page (ADR-0018/0036).

    Pure and deterministic given *today* (an ISO date, e.g. ``"2026-06-14"``) so the
    bucketing is unit-testable without a clock. Empty columns are dropped; a board-level
    **Add task** action is always offered. When *lists* (``(list_id, title)`` pairs for the
    operator's enabled writable lists) is given, the Add form gains a list (category) picker
    preselecting *default_list_id*; with none, adds go to the default list. With **two or
    more** lists each task's Edit form also gains a List picker that moves it (ADR-0038).
    """
    # A move needs somewhere to move to, so the per-task List picker appears only with ≥2
    # writable lists. (Local-only tasks never reach here with a picker — see ADR-0038.)
    move_lists = lists if lists and len(lists) >= 2 else None
    grouped: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in _BUCKET_ORDER}
    for task in tasks:
        bucket = _bucket_for(task, today)
        grouped[bucket].append(_task_card(task, bucket, move_lists=move_lists))

    columns = [
        {
            "id": bucket.lower().replace(" ", "-"),
            "title": bucket,
            "cards": grouped[bucket],
        }
        for bucket in _BUCKET_ORDER
        if grouped[bucket]
    ]
    add_action: dict[str, Any] = {
        "tool": "tasks_add",
        "label": "Add task",
        "intent": "primary",
        "icon": "plus",
        "form": True,
        "fields": ["title", "notes", "due", "priority", "tags"],
        "field_options": _TASK_FIELD_OPTIONS,
    }
    if lists:
        # Offer a list (category) picker: a labeled choice whose value is the list id and
        # label its title — the shell renders `field_choices` as a label≠value <select>
        # (ADR-0036), distinct from `field_options`' plain string enums (priority/status).
        add_action["fields"] = ["title", "list_id", "notes", "due", "priority", "tags"]
        add_action["field_choices"] = {
            "list_id": [{"value": list_id, "label": title} for list_id, title in lists],
        }
        if default_list_id is not None:
            add_action["form_values"] = {"list_id": default_list_id}
    return {
        "title": "Tasks",
        "columns": columns,
        "actions": [add_action],
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
