"""Tasks module — provider-agnostic MCP tool surface (ADR-0016).

Also serves the **Tasks** left-nav page: a core-rendered ``board`` archetype
(ADR-0018). The module supplies data only — :func:`build_tasks_board` groups tasks
into columns by the operator's chosen dimension (due date / status / priority / list,
or a flat list) and *Show* filter (open / completed / all), declares those choices as
**view controls** (ADR-0049), and attaches per-card actions that invoke the module's
own MCP tools through the core (complete / reopen / edit) plus a board-level add — and
the shell renders it. No markup ever leaves this module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from epicurus_core import (
    LOCAL_ACCOUNT,
    Account,
    AccountsView,
    Collection,
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
from epicurus_tasks.models import (
    VALID_PRIORITIES,
    VALID_STATUSES,
    VALID_TASK_SCOPES,
    Task,
    TaskScope,
)
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
        version="0.13.0",
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
    async def tasks_create_list(title: str) -> Collection:
        """Create a new task list under your connected Google account.

        Requires a connected Google account — the local store is a single implicit list
        and has no way to create named lists of its own (#474). Use the returned
        ``collection`` id right away as ``list_id`` on ``tasks_add`` or ``to_list_id`` on
        ``tasks_update`` — no need to call ``tasks_lists`` again first for that. It won't
        appear as a board category or in ``tasks_lists`` itself, though, until the operator
        enables it once in the connected-accounts Lists section (same as any other newly
        discovered Google list) — mention that if they expect to see it there.

        Args:
            title: The new list's display name.

        Returns the created list (``account``/``collection``/``title``/``writable``).
        """
        try:
            return await provider.create_list(tenant_id, title)
        except (GoogleTasksError, ValueError, NotImplementedError) as exc:
            raise RuntimeError(str(exc)) from exc

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
            list_id: The list containing the task.  Omit to have it looked up across
                your lists — you don't need to know which one it's in.

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
        value — pass **at least one** field (or ``to_list_id``), or this raises an
        error rather than silently doing nothing. To mark a task done use
        ``tasks_complete`` — this tool edits content.

        To **clear** the due date or notes, pass an empty string: ``due=""`` removes
        the due date, ``notes=""`` removes the notes. Omitting a field is different
        from clearing it — omitting leaves it unchanged, ``""`` blanks it out.

        To **move** the task to another list, pass ``to_list_id`` (a list id from
        ``tasks_lists``). On Google Tasks a move recreates the task in the target list —
        it gets a new id, and subtasks/ordering aren't carried over.

        Args:
            task_id: The provider-specific task identifier (from ``tasks_list``).
            title: New title.  Omit to leave it unchanged.
            notes: New free-text notes.  Omit to leave them unchanged; pass ``""`` to clear.
            due: New due date as an ISO date string, e.g. ``"2025-01-15"``.  Omit
                to leave it unchanged; pass ``""`` to clear it.
            priority: New priority (``"low"``/``"medium"``/``"high"``).  Omit to
                leave unchanged.  Google Tasks ignores this field.
            tags: New comma-separated tags, e.g. ``"work, urgent"``.  Omit to leave
                unchanged.  Google Tasks ignores this field.
            status: New status (``"open"``/``"in_progress"``/``"done"``).  Omit to
                leave unchanged.
            list_id: The list the task currently lives in.  Omit to have it looked up
                across your lists — you don't need to know which one it's in.
            to_list_id: Move the task to this list.  Omit to leave it where it is; when
                equal to its current list it's a no-op move (a normal edit).

        Returns the updated :class:`Task`.
        """
        if (
            title is None
            and notes is None
            and due is None
            and priority is None
            and tags is None
            and status is None
            and to_list_id is None
        ):
            raise RuntimeError(
                "nothing to change — pass at least one field to edit;"
                ' to clear the due date pass due="", or to clear notes pass notes=""'
            )
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

    @module.tool()
    async def tasks_delete(task_id: str, list_id: str | None = None) -> str:
        """Delete a task permanently.

        This removes the task entirely and cannot be undone — unlike ``tasks_complete``,
        which keeps the task but marks it done. Get the ``task_id`` from ``tasks_list``.

        Args:
            task_id: The provider-specific task identifier (from ``tasks_list``).
            list_id: The list containing the task.  Omit to have it looked up across
                your lists — you don't need to know which one it's in.

        Returns a short confirmation string.
        """
        try:
            await provider.delete_task(tenant_id, task_id, list_id=list_id)
        except (GoogleTasksError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc
        return "Task deleted."

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


# ── Tasks page: the `board` archetype data (ADR-0018 / ADR-0049) ─────────────────
#
# The module supplies data only; the core shell renders it. The board groups tasks into
# columns by the operator's chosen *group-by* dimension and *Show* filter — declared as
# **view controls** the shell renders as a toolbar (ADR-0049) — and attaches per-card
# actions that the shell turns into buttons, each invoking one of this module's MCP tools
# through the core (validated against the manifest), so mutations never bypass the contract.

_BUCKET_ORDER = ("Overdue", "Today", "Upcoming", "No date")
_BUCKET_TONE = {"Overdue": "danger", "Today": "accent"}

# Grouping dimensions (ADR-0049). Each is a board column layout the operator can switch to.
# "list" is offered only when there are named lists (categories) to group by; "none" is a
# single flat column (a plain list view).
_PRIORITY_ORDER = ("High", "Medium", "Low", "No priority")
_STATUS_COLUMN = {"open": "Open", "in_progress": "In progress", "done": "Completed"}
_STATUS_ORDER = ("Open", "In progress", "Completed")
_FLAT_COLUMN = "All tasks"
_LIST_FALLBACK = "Personal"  # category label for the silent local default (mirrors the router)

# Group-by options the *Group by* control offers, in display order. The "List" option is
# spliced in (before "None") only when the board has named lists to group by.
_GROUP_OPTIONS: tuple[tuple[str, str], ...] = (
    ("due", "Due date"),
    ("status", "Status"),
    ("priority", "Priority"),
    ("none", "None"),
)
_GROUP_LIST_OPTION = ("list", "List")
_VALID_GROUPS: frozenset[str] = frozenset({"due", "status", "priority", "list", "none"})
_DEFAULT_GROUP = "due"

# Show-filter options the *Show* control offers (the task scope passed to the providers).
_SCOPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("open", "Open"),
    ("done", "Completed"),
    ("all", "All"),
)

# field_options teaches the shell's SchemaForm which values are valid for enum-like
# string fields.  The shell overlays these onto the tool's raw JSON schema so it
# can render a <select> instead of a free-text input (ADR-0018 board extension).
_TASK_FIELD_OPTIONS: dict[str, list[str]] = {
    "priority": ["low", "medium", "high"],
    "status": ["open", "in_progress", "done"],
}


def coerce_group(value: str | None) -> str:
    """Clamp a ``group`` query param to a known grouping, defaulting to due-date (ADR-0049)."""
    return value if value in _VALID_GROUPS else _DEFAULT_GROUP


def coerce_scope(value: str | None) -> TaskScope:
    """Clamp a ``show`` query param to a known task scope, defaulting to open (ADR-0049)."""
    return cast(TaskScope, value) if value in VALID_TASK_SCOPES else "open"


def _slug(title: str) -> str:
    """A stable column id from a human title (ids aren't user-visible)."""
    return title.lower().replace(" ", "-")


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


def _column_of(task: Task, group_by: str, today: str) -> str:
    """The column title *task* falls under for the active *group_by* (ADR-0049)."""
    if group_by == "status":
        return _STATUS_COLUMN.get(task.status, task.status)
    if group_by == "priority":
        return task.priority.capitalize() if task.priority else "No priority"
    if group_by == "list":
        return task.list_title or _LIST_FALLBACK
    if group_by == "none":
        return _FLAT_COLUMN
    return _bucket_for(task, today)


def _column_order(group_by: str, lists: list[tuple[str, str]] | None) -> list[str] | None:
    """Canonical column order for *group_by*, or ``None`` to order by first appearance.

    Due / priority / status have a fixed, meaningful order; the flat "none" view is a single
    column. Grouping by **list** orders columns by the operator's *lists* (their enabled
    order), with any extra category (e.g. the local "Personal" column) appended as it appears.
    """
    if group_by == "status":
        return list(_STATUS_ORDER)
    if group_by == "priority":
        return list(_PRIORITY_ORDER)
    if group_by == "none":
        return [_FLAT_COLUMN]
    if group_by == "list":
        seed = [title for _, title in (lists or [])]
        return seed or None  # extras (Personal / untitled) are appended in _group_columns
    return list(_BUCKET_ORDER)


def _group_columns(
    tasks: list[Task],
    *,
    group_by: str,
    today: str,
    move_lists: list[tuple[str, str]] | None,
    lists: list[tuple[str, str]] | None,
) -> list[dict[str, Any]]:
    """Group *tasks* into ordered board columns by the active *group_by* (ADR-0049).

    Empty columns are dropped. Columns follow the dimension's canonical order; for the
    *list* grouping any category not in the operator's *lists* (e.g. "Personal") is appended
    in first-seen order so nothing is lost.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    appeared: list[str] = []
    for task in tasks:
        title = _column_of(task, group_by, today)
        if title not in grouped:
            grouped[title] = []
            appeared.append(title)
        grouped[title].append(_task_card(task, today=today, move_lists=move_lists))

    base = _column_order(group_by, lists)
    # Canonical order first; any category not in it (e.g. "Personal" for list grouping) is
    # appended in first-seen order so nothing is dropped.
    order = appeared if base is None else [*base, *(t for t in appeared if t not in base)]
    return [
        {"id": _slug(title), "title": title, "cards": grouped[title]}
        for title in order
        if grouped.get(title)
    ]


def _task_card(
    task: Task, *, today: str, move_lists: list[tuple[str, str]] | None = None
) -> dict[str, Any]:
    """One board card: the task plus its primary (complete / reopen) and edit actions.

    The due-date badge tone always reflects the task's *own* due bucket (overdue / today),
    independent of the active grouping. Each card carries ``list_id`` (the list the task
    belongs to) in its action args so a mutation routes back to the owning list when the
    board aggregates several lists (ADR-0036); a per-card category tag names that list. A
    **completed** card offers *Reopen* in place of *Complete* (both one-tap) and is marked
    ``done`` so the shell strikes it through. When *move_lists* is given (more than one
    writable list exists) the Edit form gains a **List** picker, prefilled to the task's
    current list, that moves the task when changed (ADR-0038).
    """
    due_bucket = _bucket_for(task, today)
    badges: list[dict[str, str]] = []
    if task.due:
        badges.append({"label": task.due[:10], "tone": _BUCKET_TONE.get(due_bucket, "dim")})
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

    done = task.status == "done"
    # Open → Complete (flip done); completed → Reopen (edit status back to open). Both one-tap.
    primary_action: dict[str, Any] = (
        {
            "tool": "tasks_update",
            "label": "Reopen",
            "icon": "rotate",
            "args": {**args, "status": "open"},
        }
        if done
        else {"tool": "tasks_complete", "label": "Complete", "icon": "check", "args": args}
    )
    # Permanent delete (#336): destructive, so it carries intent=danger + a confirm prompt the
    # shell gates behind a dialog (ActionControl/Confirm). It routes to the same tasks_delete
    # tool the chat agent can call, so the contract has one delete path (validated by the core).
    delete_action: dict[str, Any] = {
        "tool": "tasks_delete",
        "label": "Delete",
        "icon": "trash",
        "intent": "danger",
        "confirm": "Delete this task permanently? This cannot be undone.",
        "args": args,
    }
    return {
        "id": task.id,
        "title": task.title,
        "subtitle": task.notes or None,
        "badges": badges,
        "done": done,
        "actions": [primary_action, edit_action, delete_action],
    }


def _board_controls(
    *, group_by: str, scope: str, lists: list[tuple[str, str]] | None
) -> list[dict[str, Any]]:
    """The board's declarative view controls (ADR-0049): *Group by* and *Show*.

    The module declares the selectable options and the current value; the shell renders
    each as a labeled selector and re-fetches the page with ``?<id>=<value>`` on change.
    The *List* grouping is offered only when there are named lists to group by.
    """
    group_options = list(_GROUP_OPTIONS)
    if lists:
        group_options.insert(len(group_options) - 1, _GROUP_LIST_OPTION)  # before "None"
    return [
        {
            "id": "group",
            "label": "Group by",
            "value": group_by,
            "options": [{"value": value, "label": label} for value, label in group_options],
        },
        {
            "id": "show",
            "label": "Show",
            "value": scope,
            "options": [{"value": value, "label": label} for value, label in _SCOPE_OPTIONS],
        },
    ]


def build_tasks_board(
    tasks: list[Task],
    *,
    today: str,
    group_by: str = _DEFAULT_GROUP,
    scope: str = "open",
    lists: list[tuple[str, str]] | None = None,
    default_list_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``board`` archetype payload for the Tasks page (ADR-0018 / 0036 / 0047).

    Pure and deterministic given *today* (an ISO date, e.g. ``"2026-06-14"``) so the
    grouping is unit-testable without a clock. *group_by* picks the column layout (``"due"``
    default, ``"status"``, ``"priority"``, ``"list"``, or ``"none"`` for a flat list) and
    *scope* the *Show* filter echoed into the controls (the caller has already fetched the
    matching tasks). Empty columns are dropped; the board always declares its **view
    controls** and a board-level **Add task** action. When *lists* (``(list_id, title)``
    pairs for the operator's enabled writable lists) is given, the Add form gains a list
    (category) picker preselecting *default_list_id*, the *Group by* control offers
    **List**, and a board-level **New list** action (``tasks_create_list``) appears
    (#474) — *lists* non-empty is a reliable proxy for "an external account is
    connected" (ADR-0031 auto-enables every discovered list on connect), so the same
    condition gates both. A list created this way exists on Google immediately (and its
    id is immediately usable for `tasks_add`/`tasks_update`), but — like any other newly
    discovered collection — needs the operator's one-time toggle in the connected-accounts
    Lists section before it appears as a board category or in this picker; auto-enabling
    it here would need the module to write the operator's collection prefs, which it has
    no path to do today. With two or more lists each task's Edit form also gains a List
    picker that moves it (ADR-0038).
    """
    # Grouping by list needs named lists; with none, fall back to the due-date layout so the
    # control and the columns stay consistent.
    if group_by == "list" and not lists:
        group_by = _DEFAULT_GROUP
    # A move needs somewhere to move to, so the per-task List picker appears only with ≥2
    # writable lists. (Local-only tasks never reach here with a picker — see ADR-0038.)
    move_lists = lists if lists and len(lists) >= 2 else None
    columns = _group_columns(
        tasks, group_by=group_by, today=today, move_lists=move_lists, lists=lists
    )
    add_action: dict[str, Any] = {
        "tool": "tasks_add",
        "label": "Add task",
        "intent": "primary",
        "icon": "plus",
        # Render as a compact icon-only "+" (#337); the label moves to a tooltip/aria-label.
        "icon_only": True,
        "form": True,
        "fields": ["title", "notes", "due", "priority", "tags"],
        "field_options": _TASK_FIELD_OPTIONS,
    }
    actions = [add_action]
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
        # "New list" needs a connected external account to create against (Google, today);
        # *lists* being non-empty is a reliable proxy for that (connecting auto-enables every
        # discovered list, ADR-0031), so this reuses the same gate as the list picker above
        # rather than a separate is-connected check (#474).
        actions.append(
            {
                "tool": "tasks_create_list",
                "label": "New list",
                "icon": "folder",
                "form": True,
                "fields": ["title"],
            }
        )
    return {
        "title": "Tasks",
        "columns": columns,
        "controls": _board_controls(group_by=group_by, scope=scope, lists=lists),
        "actions": actions,
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
