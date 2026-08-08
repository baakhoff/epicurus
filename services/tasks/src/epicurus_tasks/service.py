"""Tasks module — provider-agnostic MCP tool surface (ADR-0016).

Also serves two left-nav pages, both core-rendered ``board`` archetypes (ADR-0018).
The module supplies data only — no markup ever leaves this module:

- **Tasks** (:func:`build_tasks_board`): the *scheduled* picture. Groups **dated** tasks
  into columns by the operator's chosen dimension (due date / status / priority / list,
  or a flat list) and *Show* filter (open / completed / all), declares those choices as
  **view controls** (ADR-0049), and attaches per-card actions that invoke the module's
  own MCP tools through the core (complete / reopen / edit) plus a board-level add.
- **Can** (:func:`build_tasks_can`, #766): the backlog — every task **without** a due
  date, in one flat column, partitioned out of the board entirely so the board is always
  a clean picture of what's actually scheduled. Its Add creates undated tasks and each
  card carries a one-tap **Schedule** action (a due-only form) that places the task on
  the board; clearing a task's due date moves it back. A pure read-partition — no
  provider contract change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, cast

from pydantic import Field

from epicurus_core import (
    LOCAL_ACCOUNT,
    Account,
    AccountsView,
    AutomationTemplate,
    Collection,
    CollectionPrefs,
    CollectionsSpec,
    EntityRef,
    EpicurusModule,
    HoverCard,
    HoverCardDetail,
    HoverCardLink,
    PageSpec,
    UiSection,
    capped_listing,
    event_subject,
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
from epicurus_tasks.recurrence import validate_rrule

log = get_logger("epicurus_tasks.service")

MODULE_NAME = "tasks"
TASKS_PAGE_ID = "board"
"""The id of the Tasks left-nav page; forms its nav route and data path."""

CAN_PAGE_ID = "can"
"""The id of the Can left-nav page (#766) — the undated-task backlog."""

# An async hook returning the operator's enabled writable lists as ``(id, title)`` pairs.
# Backs the ``tasks_lists`` tool so the chat agent can discover lists and pick one when
# adding/moving (the web pickers get the same data via the page). ``None`` in unit tests.
ListCategories = Callable[[], Awaitable[list[tuple[str, str]]]]

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

# The recurrence rule (#471, ADR-0082). Agent-facing: a bare RRULE; the web form renders the
# friendly repeat picker instead (via the ``format: rrule`` hint the shell's SchemaForm reads).
_REPEAT_DESCRIPTION = (
    "Repeat rule (RFC 5545 RRULE, no leading 'RRULE:'), e.g. 'FREQ=WEEKLY' or"
    " 'FREQ=DAILY;COUNT=10'. Makes the task recurring: completing it creates the next"
    " instance (a due date is required to anchor the rule). Omit for a one-off task; pass"
    " an empty string to remove an existing rule."
)
# ``json_schema_extra={"format": "rrule"}`` tells the web SchemaForm to render the shared
# repeat picker (None/Daily/Weekdays/Weekly/Monthly/Yearly/Custom) rather than a raw text box;
# the submitted value is still a bare RRULE string, so the agent tool surface is unchanged.
_Repeat = Annotated[
    str, Field(json_schema_extra={"format": "rrule"}, description=_REPEAT_DESCRIPTION)
]

# The due date rides the same `format` seam (ADR-0082's precedent): ``format: "date"`` makes
# the shell's SchemaForm render a native date picker emitting a floating ``YYYY-MM-DD``. The
# descriptions double as the form's field hint and the agent's parameter doc — both say where
# an undated task goes (the Can, #766), so nothing ever vanishes silently from the board.
_ADD_DUE_DESCRIPTION = (
    'Due date as an ISO date, e.g. "2026-01-15". A task without one is saved to the Can'
    " — the undated backlog page — instead of the board, until it's scheduled."
)
_UPDATE_DUE_DESCRIPTION = (
    'New due date as an ISO date, e.g. "2026-01-15". Pass "" to clear it — the task then'
    " moves off the board into the Can (the undated backlog page)."
)
_AddDue = Annotated[
    str, Field(json_schema_extra={"format": "date"}, description=_ADD_DUE_DESCRIPTION)
]
_UpdateDue = Annotated[
    str, Field(json_schema_extra={"format": "date"}, description=_UPDATE_DUE_DESCRIPTION)
]

# Tags ride the same seam (#763): ``format: "tags"`` makes the shell's SchemaForm render a
# **chips input** — existing tags as removable chips inside the box, typeahead over the
# module-supplied suggestions (``field_suggestions``), Enter/comma committing a new tag —
# instead of a bare text input. The submitted value is still the comma-separated string,
# so the MCP contract is unchanged (agents keep sending ``"work, urgent"``).
_TAGS_DESCRIPTION = (
    'Comma-separated labels, e.g. "work, urgent". Tags are local-only — Google Tasks has'
    " no equivalent field and ignores them."
)
_Tags = Annotated[str, Field(json_schema_extra={"format": "tags"}, description=_TAGS_DESCRIPTION)]

# Short labels for a repeat rule, for the board badge / hover-card. Best-effort: an exotic
# custom rule falls back to "Custom" (the picker still round-trips the exact RRULE).
_FREQ_LABEL: dict[str, str] = {
    "DAILY": "Daily",
    "WEEKLY": "Weekly",
    "MONTHLY": "Monthly",
    "YEARLY": "Yearly",
}
_WEEKDAYS_BYDAY = "MO,TU,WE,TH,FR"


def _repeat_label(rule: str) -> str:
    """A short human label for an RRULE: 'Daily' / 'Weekdays' / 'Weekly' / … / 'Custom'."""
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = parts.get("FREQ", "").upper()
    if freq == "WEEKLY" and parts.get("BYDAY", "").upper() == _WEEKDAYS_BYDAY:
        return "Weekdays"
    return _FREQ_LABEL.get(freq, "Custom")


def _repeat_summary(rule: str) -> str:
    """A phrase for the board badge, e.g. 'Repeats weekly' / 'Repeats on weekdays'."""
    label = _repeat_label(rule)
    if label == "Weekdays":
        return "Repeats on weekdays"
    if label == "Custom":
        return "Repeats"
    return f"Repeats {label.lower()}"


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
        version="0.21.0",
        description=(
            "Task management: list, add, edit, complete, and repeat tasks. Backed by a local"
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
            ),
            # The Can (#766): the undated-task backlog, right under the board in the nav.
            # Same `board` archetype — one flat column, its own Add / Schedule actions.
            PageSpec(
                id=CAN_PAGE_ID,
                title="Can",
                archetype="board",
                icon="inbox",
                nav_order=41,
            ),
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
        # Starter presets for the Templates tab (#705, ADR-0105) — never auto-instantiated.
        automation_templates=[
            AutomationTemplate(
                key="due-today-digest",
                name="Morning due-today digest",
                description="A short daily summary of what's due today across your lists.",
                trigger={"cadence": "daily", "hour": 8},
                prompt="List today's due tasks and summarize what's on deck for today.",
                autonomy="notify",
                sinks=["push"],
            ),
            AutomationTemplate(
                key="on-task-overdue",
                name="Tell me when a task goes overdue",
                description="Runs whenever an open task's due date passes.",
                trigger={"module": MODULE_NAME, "event_type": "tasks.task_overdue"},
                prompt="A task is now overdue. Name it and its original due date in one sentence.",
                autonomy="notify",
                sinks=["push"],
            ),
        ],
    )

    # Module event spine (#664, ADR-0103).
    module.emits(
        event_subject("tasks.task_created"),
        "A new task was created through this module (#664).",
    )
    module.emits(
        event_subject("tasks.task_completed"),
        "A task was marked done (#664).",
    )
    module.emits(
        event_subject("tasks.task_updated"),
        "An existing task was edited (#664).",
    )
    module.emits(
        event_subject("tasks.task_moved"),
        "A task moved between lists (ADR-0038, #664).",
    )
    module.emits(
        event_subject("tasks.task_due_soon"),
        "An open task is within its configured lead time of its due date (default 1 day, #664).",
    )
    module.emits(
        event_subject("tasks.task_overdue"),
        "An open task's due date has passed (#664).",
    )

    @module.tool(side_effect="read")
    async def tasks_list(list_id: str | None = None) -> str:
        """List open tasks from the active provider as entity-reference chips.

        Returns the tasks as entity-reference chips (ADR-0019): hover a chip for the
        task's hover-card, click it to open the task in the side panel. Each chip
        carries the task id, so you can refer to a task later without listing again.
        The accompanying text lists each task's title and due date. Reading the list
        also runs the overdue-recurrence sweep (#515): any open repeating task whose
        due date has passed spawns its next occurrence (the rule moves to the new
        task) before the list is returned.

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
        # Capped the same way as the entity-ref id block the core appends (both default to
        # LIST_CAP, #468) — a long backlog can otherwise inflate the text with hundreds of
        # lines (#539, matching calendar's #522 adoption). ``noun="open task"`` keeps the
        # pre-cap "Found N open task(s):" header — tasks_list only ever returns open tasks, so
        # the plain "task(s)" the cap defaulted to read as if completed ones were included (#553).
        text = capped_listing(lines, noun="open task")
        return tool_envelope(text, refs)

    @module.tool(side_effect="read")
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
        due: _AddDue | None = None,
        priority: str | None = None,
        tags: _Tags | None = None,
        status: str = "open",
        list_id: str | None = None,
        repeat: _Repeat | None = None,
    ) -> Task:
        """Create a new task.

        A task **without a due date is filed into the Can** — the undated backlog page —
        rather than the board, so "note down: buy a drill" lands there until it's
        scheduled (#766). Mention that when confirming an undated add. If more than one
        task list exists and the user hasn't said which to use, call ``tasks_lists``
        first and ask which list, then pass its id as ``list_id``.

        Args:
            title: Task title (required).
            notes: Optional free-text notes or description.
            due: Optional due date as an ISO date string, e.g. ``"2025-01-15"``.
                Omit it to file the task in the Can (the undated backlog) instead of
                the board.
            priority: Optional priority level — ``"low"``, ``"medium"``, or ``"high"``.
                Google Tasks ignores this field.
            tags: Optional comma-separated labels, e.g. ``"work, urgent"``.
                Google Tasks ignores this field.
            status: Initial status — ``"open"`` (default), ``"in_progress"``, or
                ``"done"``.  Google Tasks maps ``"done"`` to completed;
                ``"in_progress"`` is local-only and reads back as ``"open"`` from Google.
            list_id: Target list identifier.  Omit for the default list.
            repeat: Optional RFC 5545 RRULE making the task recurring (e.g. ``"FREQ=WEEKLY"``).
                Completing the task creates the next instance. Requires a ``due`` to anchor the
                rule. Emulated module-side and works on both providers (#471, ADR-0082).

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
        if repeat:
            if not due:
                raise RuntimeError("a recurring task needs a due date to anchor the repeat rule")
            try:
                validate_rrule(repeat)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
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
                repeat=repeat,
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
        due: _UpdateDue | None = None,
        priority: str | None = None,
        tags: _Tags | None = None,
        status: str | None = None,
        list_id: str | None = None,
        to_list_id: str | None = None,
        repeat: _Repeat | None = None,
    ) -> Task:
        """Edit an existing task's title, notes, due date, priority, tags, status, or repeat.

        Only the fields you pass are changed; omitted fields keep their current
        value — pass **at least one** field (or ``to_list_id``), or this raises an
        error rather than silently doing nothing. To mark a task done use
        ``tasks_complete`` — this tool edits content.

        To **clear** the due date, notes, or repeat rule, pass an empty string: ``due=""``
        removes the due date, ``notes=""`` removes the notes, ``repeat=""`` makes a recurring
        task one-off. Omitting a field is different from clearing it — omitting leaves it
        unchanged, ``""`` blanks it out.

        To **move** the task to another list, pass ``to_list_id`` (a list id from
        ``tasks_lists``). On Google Tasks a move recreates the task in the target list —
        it gets a new id, and subtasks/ordering aren't carried over.

        Args:
            task_id: The provider-specific task identifier (from ``tasks_list``).
            title: New title.  Omit to leave it unchanged.
            notes: New free-text notes.  Omit to leave them unchanged; pass ``""`` to clear.
            due: New due date as an ISO date string, e.g. ``"2025-01-15"``.  Omit
                to leave it unchanged; pass ``""`` to clear it — the task then moves off
                the board into the Can (the undated backlog, #766). Clearing is rejected
                if the task has a live ``repeat`` rule and this call doesn't also touch
                ``repeat``, since clearing the anchor would strand the recurrence (#534).
                Setting a due date on a Can task schedules it onto the board.
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
            repeat: New RFC 5545 RRULE (e.g. ``"FREQ=WEEKLY"``).  Omit to leave unchanged;
                pass ``""`` to remove an existing rule (#471, ADR-0082).  Setting a rule
                requires the task to have (or be given in the same call) a due date to
                anchor it — otherwise the update is rejected.

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
            and repeat is None
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
        if repeat:
            if due == "":
                raise RuntimeError(
                    "a recurring task needs a due date — don't clear due with a repeat"
                )
            try:
                validate_rrule(repeat)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            if due is None:
                # repeat is being set without a due in *this* call — the anchor must already
                # be on the task, or the rule silently never materializes (#515). tasks_add's
                # due-required check can't see this case: an existing task that has no due
                # and isn't getting one in this same update.
                current = await provider.get_task(tenant_id, task_id, list_id=list_id)
                if current is None or not current.due:
                    raise RuntimeError(
                        "a recurring task needs a due date to anchor the repeat rule —"
                        ' pass due="YYYY-MM-DD" in this update, or set one first'
                    )
        elif due == "" and repeat is None:
            # The symmetric hole (#534): repeat is untouched (None) in *this* call — not
            # explicitly cleared too (repeat="", handled by simply not raising here) — but if
            # the task's rule is currently live, clearing due strands it exactly like the
            # #515 case above — the sweep skips a due-less task (`not task.due`) and a later
            # completion's `_materialize` can't compute a next occurrence with no anchor, so
            # the recurrence silently dies with no successor. The board form never hits this
            # (it always resends the current repeat alongside due), so it's the direct/agent
            # tool path that needs the guard.
            current = await provider.get_task(tenant_id, task_id, list_id=list_id)
            if current is not None and current.repeat:
                raise RuntimeError(
                    "this task has a live repeat rule — clearing its due date would strand"
                    ' the recurrence; pass repeat="" too to end the series, or give it a'
                    " new due date instead"
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
                repeat=repeat,
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


# ── Tasks + Can pages: the `board` archetype data (ADR-0018 / ADR-0049 / #766) ────
#
# The module supplies data only; the core shell renders it. The board groups tasks into
# columns by the operator's chosen *group-by* dimension and *Show* filter — declared as
# **view controls** the shell renders as a toolbar (ADR-0049) — and attaches per-card
# actions that the shell turns into buttons, each invoking one of this module's MCP tools
# through the core (validated against the manifest), so mutations never bypass the contract.
#
# The two pages partition every task by whether it has a due date (#766): the board shows
# only *dated* tasks (under every grouping — there is no "No date" bucket any more) and the
# Can holds the rest, so scheduling is exactly "give it a due date" and un-scheduling is
# "clear it". The partition is read-side only; providers are untouched.

_BUCKET_ORDER = ("Overdue", "Today", "Upcoming")
_BUCKET_TONE = {"Overdue": "danger", "Today": "accent"}

# Grouping dimensions (ADR-0049). Each is a board column layout the operator can switch to.
# "list" is offered only when there are named lists (categories) to group by; "none" is a
# single flat column (a plain list view).
_PRIORITY_ORDER = ("High", "Medium", "Low", "No priority")
_STATUS_COLUMN = {"open": "Open", "in_progress": "In progress", "done": "Completed"}
_STATUS_ORDER = ("Open", "In progress", "Completed")
_FLAT_COLUMN = "All tasks"
_LIST_FALLBACK = "Personal"  # category label for the silent local default (mirrors the router)

# Group-by options the *Group by* control offers, in display order: the three fixed
# dimensions, then the conditional ones — "List" only when the board has named lists to
# group by and "Tags" (#763) only when a visible task actually carries a tag (a grouping
# with nothing to group by would be a dead option) — then the flat "None".
_GROUP_FIXED_OPTIONS: tuple[tuple[str, str], ...] = (
    ("due", "Due date"),
    ("status", "Status"),
    ("priority", "Priority"),
)
_GROUP_LIST_OPTION = ("list", "List")
_GROUP_TAGS_OPTION = ("tags", "Tags")
_GROUP_NONE_OPTION = ("none", "None")
_VALID_GROUPS: frozenset[str] = frozenset({"due", "status", "priority", "list", "tags", "none"})
_DEFAULT_GROUP = "due"
_UNTAGGED_COLUMN = "Untagged"

# Show-filter options the *Show* control offers (the task scope passed to the providers).
_SCOPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("open", "Open"),
    ("done", "Completed"),
    ("all", "All"),
)

# View-mode options the board's *View* switcher offers (#767): three client-side
# representations of the same fetched payload. ``view`` is a **reserved control id** — a
# board-archetype extension of ADR-0049: the shell recognizes it and renders the matching
# representation (kanban columns / a sortable flat list / a month grid) itself, because
# only the shell owns rendering. The module's part stays declarative: offer the options,
# clamp + echo the choice, and omit the *Group by* control when columns aren't what's
# shown. The card payload is identical across views — the alternate renderings read the
# structured card fields (``due`` / ``priority`` / ``tags`` / ``list_title``).
_VIEW_OPTIONS: tuple[tuple[str, str], ...] = (
    ("board", "Board"),
    ("list", "List"),
    ("calendar", "Calendar"),
)
_VALID_VIEWS: frozenset[str] = frozenset(value for value, _ in _VIEW_OPTIONS)
_DEFAULT_VIEW = "board"

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


def coerce_view(value: str | None) -> str:
    """Clamp a ``view`` query param to a known view mode, defaulting to the board (#767)."""
    return value if value in _VALID_VIEWS else _DEFAULT_VIEW


def _distinct_tags(tasks: list[Task]) -> list[str]:
    """The distinct tags across *tasks*, alphabetical (case-insensitive), first casing wins.

    The known-tags source for the add/edit forms' chips typeahead (#763,
    ``field_suggestions``): the module supplies the data, the shell renders the picker
    (ADR-0018/0019). Computed over the full fetched set — both pages suggest the union,
    so a tag used only on a Can task still autocompletes on the board and vice versa.
    """
    seen: dict[str, str] = {}
    for task in tasks:
        for tag in task.tags:
            seen.setdefault(tag.casefold(), tag)
    return sorted(seen.values(), key=str.casefold)


def _slug(title: str) -> str:
    """A stable column id from a human title (ids aren't user-visible)."""
    return title.lower().replace(" ", "-")


def calendar_feed_items(tasks: list[Task], start: str, end: str) -> list[dict[str, Any]]:
    """Open tasks due within ``[start, end)`` as calendar-feed items (#469).

    Feeds the core's generic cross-module calendar overlay — the endpoint this backs
    (``GET /calendar-feed``) has no manifest declaration; a module that doesn't serve it
    simply 404s on the core's probe and is skipped. *tasks* should already be scope
    ``"open"`` (excludes completed, ADR-0049's ``TaskScope``); a task without a due date
    is dropped here too, since an undated task has nowhere to anchor on the calendar.
    ISO date strings compare lexicographically, matching :func:`_bucket_for`'s convention.

    ``kind`` rides alongside ``module``/``ref_id`` (a small, deliberate addition beyond the
    issue's own sketch) so the shell's click handler can call the *generic*
    ``GET /resolve/{kind}/{ref_id}`` resolver (ADR-0019) without hardcoding "task" — a
    future second calendar-feed module keeps working with zero web changes.
    """
    items: list[dict[str, Any]] = []
    for task in tasks:
        if not task.due:
            continue
        due = task.due[:10]
        if start <= due < end:
            items.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "date": due,
                    "status": task.status,
                    "ref_id": task.id,
                    "kind": TASK_KIND,
                }
            )
    return items


def _bucket_for(task: Task, today: str) -> str:
    """The due-date column a task belongs in, relative to *today* (ISO date).

    ISO date strings sort lexicographically, so the comparison needs no parsing.
    "No date" is unreachable as a *column* now — the board holds dated tasks only
    (#766) — but the card builder still calls this for an undated Can card's badge
    tone (where no due badge is even added), so the branch stays for totality.
    """
    if not task.due:
        return "No date"
    due = task.due[:10]
    if due < today:
        return "Overdue"
    if due == today:
        return "Today"
    return "Upcoming"


def _columns_of(task: Task, group_by: str, today: str) -> list[str]:
    """The column title(s) *task* falls under for the active *group_by* (ADR-0049).

    Every dimension is one-column-per-task except **tags** (#763): a task appears under
    **each** of its tags (multi-membership), and an untagged task lands in "Untagged" —
    which is why this returns a list rather than a single title.
    """
    if group_by == "status":
        return [_STATUS_COLUMN.get(task.status, task.status)]
    if group_by == "priority":
        return [task.priority.capitalize() if task.priority else "No priority"]
    if group_by == "list":
        return [task.list_title or _LIST_FALLBACK]
    if group_by == "tags":
        return list(task.tags) or [_UNTAGGED_COLUMN]
    if group_by == "none":
        return [_FLAT_COLUMN]
    return [_bucket_for(task, today)]


def _column_order(group_by: str, lists: list[tuple[str, str]] | None) -> list[str] | None:
    """Canonical column order for *group_by*, or ``None`` to order by first appearance.

    Due / priority / status have a fixed, meaningful order; the flat "none" view is a single
    column. Grouping by **list** orders columns by the operator's *lists* (their enabled
    order), with any extra category (e.g. the local "Personal" column) appended as it appears.
    Grouping by **tags** is ordered separately in :func:`_group_columns` — alphabetical with
    "Untagged" last (#763) — since the tag set only exists once the tasks are grouped.
    """
    if group_by == "status":
        return list(_STATUS_ORDER)
    if group_by == "priority":
        return list(_PRIORITY_ORDER)
    if group_by == "none":
        return [_FLAT_COLUMN]
    if group_by in ("list", "tags"):
        seed = [title for _, title in (lists or [])] if group_by == "list" else []
        return seed or None  # extras (Personal / untitled) are appended in _group_columns
    return list(_BUCKET_ORDER)


def _group_columns(
    tasks: list[Task],
    *,
    group_by: str,
    today: str,
    move_lists: list[tuple[str, str]] | None,
    lists: list[tuple[str, str]] | None,
    tag_suggestions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Group *tasks* into ordered board columns by the active *group_by* (ADR-0049).

    Empty columns are dropped. Columns follow the dimension's canonical order; for the
    *list* grouping any category not in the operator's *lists* (e.g. "Personal") is appended
    in first-seen order so nothing is lost, and each list column carries its ``list_id`` so
    the shell's drag-move matches drop targets **by id, not title** (#763 — a tag column
    that happens to share a list's name must never accept a list-move drop). The *tags*
    grouping (#763) is **multi-membership** — a task appears under each of its tags, with
    untagged ones in "Untagged" — ordered alphabetically (case-insensitive), "Untagged"
    last; the same card dict is shared across a task's columns, matching the one-task
    reality behind them.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    appeared: list[str] = []
    for task in tasks:
        card = _task_card(task, today=today, move_lists=move_lists, tag_suggestions=tag_suggestions)
        for title in _columns_of(task, group_by, today):
            if title not in grouped:
                grouped[title] = []
                appeared.append(title)
            grouped[title].append(card)

    if group_by == "tags":
        # Alphabetical (case-insensitive, so "api" and "Zoo" don't split on case), stable
        # across reloads; the catch-all Untagged column always closes the board.
        order = sorted((t for t in appeared if t != _UNTAGGED_COLUMN), key=str.casefold)
        if _UNTAGGED_COLUMN in grouped:
            order.append(_UNTAGGED_COLUMN)
    else:
        base = _column_order(group_by, lists)
        # Canonical order first; any category not in it (e.g. "Personal" for list grouping)
        # is appended in first-seen order so nothing is dropped.
        order = appeared if base is None else [*base, *(t for t in appeared if t not in base)]

    # A list column is a real drop target for the shell's drag-move: name its list id so
    # the match is by id (title collisions with tag/status columns can't misfire, #763).
    list_id_of = {title: list_id for list_id, title in (lists or [])} if group_by == "list" else {}
    columns: list[dict[str, Any]] = []
    for title in order:
        if not grouped.get(title):
            continue
        column: dict[str, Any] = {"id": _slug(title), "title": title, "cards": grouped[title]}
        if title in list_id_of:
            column["list_id"] = list_id_of[title]
        columns.append(column)
    return columns


def _task_card(
    task: Task,
    *,
    today: str,
    move_lists: list[tuple[str, str]] | None = None,
    schedule: bool = False,
    tag_suggestions: list[str] | None = None,
) -> dict[str, Any]:
    """One board card: the task plus its primary (complete / reopen) and edit actions.

    The due-date badge tone always reflects the task's *own* due bucket (overdue / today),
    independent of the active grouping. Each card carries ``list_id`` (the list the task
    belongs to) in its action args so a mutation routes back to the owning list when the
    board aggregates several lists (ADR-0036); a per-card category tag names that list. A
    **completed** card offers *Reopen* in place of *Complete* (both one-tap) and is marked
    ``done`` so the shell strikes it through. When *move_lists* is given (more than one
    writable list exists) the Edit form gains a **List** picker, prefilled to the task's
    current list, that moves the task when changed (ADR-0038). With *schedule* (Can cards,
    #766) the card leads with a **Schedule** action — a due-only ``tasks_update`` form,
    prefilled to *today* — which is how a Can task is placed on the board.

    Besides the rendered badges, the card carries its **structured fields** — ``due`` (an
    ISO date), ``priority``, ``tags``, ``list_title`` — as data (#767): the shell's List
    and Calendar representations sort/place by them, which rendered badge strings can't
    support. An additive, documented extension of the board card shape; badges stay for
    the board rendering.

    Tags honesty (#763): the Edit form offers the ``tags`` chips field only for **local**
    tasks (``task.list_id is None``). A Google task's tags would be silently dropped by
    the provider (Google Tasks has no such field) — a form field that pretends to save is
    worse than no field, so it is hidden rather than allowed to no-op. When offered, the
    field carries the board's known tags as *tag_suggestions* (``field_suggestions``) for
    the shell's typeahead — module supplies data, shell renders (ADR-0018/0019).
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
    if task.repeat:  # recurring task (ADR-0082): e.g. "Repeats weekly"
        badges.append({"label": _repeat_summary(task.repeat), "tone": "accent"})

    # Local tasks only: tags persist nowhere on Google (silent provider drop), so the
    # field is honest only where the write actually lands (#763).
    tags_editable = task.list_id is None
    edit_fields = ["title", "notes", "due", "priority", "tags", "status", "repeat"]
    if not tags_editable:
        edit_fields.remove("tags")

    args = {"task_id": task.id, "list_id": task.list_id}
    edit_action: dict[str, Any] = {
        "tool": "tasks_update",
        "label": "Edit",
        "icon": "pencil",
        "form": True,
        "fields": edit_fields,
        "field_options": _TASK_FIELD_OPTIONS,
        "args": args,
        "form_values": {
            "title": task.title,
            "notes": task.notes or "",
            "due": task.due or "",
            "priority": task.priority or "",
            "status": task.status,
            "repeat": task.repeat or "",
        },
    }
    if tags_editable:
        edit_action["form_values"]["tags"] = ", ".join(task.tags)
        if tag_suggestions:
            edit_action["field_suggestions"] = {"tags": tag_suggestions}
    if move_lists:
        # The List picker is the move target (`to_list_id`); the source stays in `args`.
        edit_action["fields"] = ["title", "to_list_id", *edit_fields[1:]]
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
    actions = [primary_action, edit_action, delete_action]
    if schedule:
        # The Can's headline verb (#766): a one-tap form with just the due picker
        # (`format: "date"` renders the shell's native date field), prefilled to today so
        # opening and submitting schedules for today; picking another date wins. Setting
        # the due date is what moves the task onto the board.
        actions.insert(
            0,
            {
                "tool": "tasks_update",
                "label": "Schedule",
                "icon": "calendar",
                "intent": "primary",
                "form": True,
                "fields": ["due"],
                "args": args,
                "form_values": {"due": today},
            },
        )
    return {
        "id": task.id,
        "title": task.title,
        "subtitle": task.notes or None,
        "badges": badges,
        "done": done,
        "actions": actions,
        # Structured card fields (#767) — data, not presentation: the shell's List view
        # sorts by them and the Calendar view places by `due`. `due` is the bare ISO date
        # (a provider may store a full RFC 3339 timestamp; only the date places a card).
        "due": task.due[:10] if task.due else None,
        "priority": task.priority,
        "tags": list(task.tags),
        "list_title": task.list_title,
    }


def _show_control(scope: str) -> dict[str, Any]:
    """The *Show* (task scope) view control — shared by the board and the Can (#766)."""
    return {
        "id": "show",
        "label": "Show",
        "value": scope,
        "options": [{"value": value, "label": label} for value, label in _SCOPE_OPTIONS],
    }


def _board_controls(
    *,
    view: str,
    group_by: str,
    scope: str,
    lists: list[tuple[str, str]] | None,
    has_tags: bool = False,
) -> list[dict[str, Any]]:
    """The board's declarative view controls (ADR-0049 / #767): *View*, *Group by*, *Show*.

    The module declares the selectable options and the current value; the shell renders
    each as a labeled selector — except the reserved ``view`` control, which it renders as
    its standard view switcher — and re-fetches the page with ``?<id>=<value>`` on change.
    The *List* grouping is offered only when there are named lists to group by, and *Tags*
    (#763) only when a visible task actually carries a tag (*has_tags*) — both would
    otherwise be dead options. The *Group by* control shows only under the **Board**
    view — grouping shapes kanban columns; the List and Calendar representations are
    flat/date-keyed and would render it as a dead knob (#767). *Show* applies everywhere.
    """
    controls: list[dict[str, Any]] = [
        {
            "id": "view",
            "label": "View",
            "value": view,
            "options": [{"value": value, "label": label} for value, label in _VIEW_OPTIONS],
        }
    ]
    if view == _DEFAULT_VIEW:
        group_options = list(_GROUP_FIXED_OPTIONS)
        if lists:
            group_options.append(_GROUP_LIST_OPTION)
        if has_tags:
            group_options.append(_GROUP_TAGS_OPTION)
        group_options.append(_GROUP_NONE_OPTION)
        controls.append(
            {
                "id": "group",
                "label": "Group by",
                "value": group_by,
                "options": [{"value": value, "label": label} for value, label in group_options],
            }
        )
    controls.append(_show_control(scope))
    return controls


def build_tasks_board(
    tasks: list[Task],
    *,
    today: str,
    view: str = _DEFAULT_VIEW,
    group_by: str = _DEFAULT_GROUP,
    scope: str = "open",
    lists: list[tuple[str, str]] | None = None,
    default_list_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``board`` archetype payload for the Tasks page (ADR-0018 / 0036 / 0047).

    Pure and deterministic given *today* (an ISO date, e.g. ``"2026-06-14"``) so the
    grouping is unit-testable without a clock. *view* is the operator's chosen
    representation (#767) — echoed into the *View* control and deciding whether *Group by*
    is offered; the payload itself is identical across views (the shell renders the
    switch, reading each card's structured fields). *group_by* picks the column layout
    (``"due"`` default, ``"status"``, ``"priority"``, ``"list"``, ``"tags"`` — a task
    under **each** of its tags, #763 — or ``"none"`` for a flat list) and *scope* the
    *Show* filter echoed into the controls (the caller has already fetched the matching
    tasks). Empty columns are dropped; the board always declares its **view
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

    The board shows **dated tasks only** (#766): undated ones are partitioned into the Can
    (:func:`build_tasks_can`) under every grouping, so there is no "No date" bucket and the
    board is always the picture of what's actually scheduled. Callers pass the full fetched
    list; the partition happens here so no caller can accidentally leak backlog onto it.

    Tags (#763): *Group by → Tags* is offered only when a visible (dated) task actually
    carries one; the known tags across the **full** fetched set feed the add/edit chips
    typeahead (``field_suggestions``). The Add form offers the ``tags`` field only when
    the add targets the local store — with *lists* given, every add lands on a Google
    list, where tags would be silently dropped (no such provider field), so the field is
    hidden rather than allowed to pretend (the same honesty rule as the per-card Edit).
    """
    tag_suggestions = _distinct_tags(tasks)  # full set (#763): board + Can suggest the union
    tasks = [t for t in tasks if t.due]
    has_tags = any(task.tags for task in tasks)
    # Grouping by list needs named lists, and by tags needs a visible tag; with none, fall
    # back to the due-date layout so the control and the columns stay consistent.
    if (group_by == "list" and not lists) or (group_by == "tags" and not has_tags):
        group_by = _DEFAULT_GROUP
    # A move needs somewhere to move to, so the per-task List picker appears only with ≥2
    # writable lists. (Local-only tasks never reach here with a picker — see ADR-0038.)
    move_lists = lists if lists and len(lists) >= 2 else None
    columns = _group_columns(
        tasks,
        group_by=group_by,
        today=today,
        move_lists=move_lists,
        lists=lists,
        tag_suggestions=tag_suggestions,
    )
    add_action: dict[str, Any] = {
        "tool": "tasks_add",
        "label": "Add task",
        "intent": "primary",
        "icon": "plus",
        # Render as a compact icon-only "+" (#337); the label moves to a tooltip/aria-label.
        "icon_only": True,
        "form": True,
        "fields": ["title", "notes", "due", "priority", "tags", "repeat"],
        "field_options": _TASK_FIELD_OPTIONS,
    }
    if tag_suggestions:
        add_action["field_suggestions"] = {"tags": tag_suggestions}
    actions = [add_action]
    if lists:
        # Offer a list (category) picker: a labeled choice whose value is the list id and
        # label its title — the shell renders `field_choices` as a label≠value <select>
        # (ADR-0036), distinct from `field_options`' plain string enums (priority/status).
        # The picker lists *external* writable lists only, so every add here lands on
        # Google — drop the tags field rather than let it silently no-op (#763).
        add_action["fields"] = ["title", "list_id", "notes", "due", "priority", "repeat"]
        add_action.pop("field_suggestions", None)
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
        "controls": _board_controls(
            view=view, group_by=group_by, scope=scope, lists=lists, has_tags=has_tags
        ),
        "actions": actions,
    }


_CAN_COLUMN = "Backlog"


def build_tasks_can(
    tasks: list[Task],
    *,
    today: str,
    scope: str = "open",
    lists: list[tuple[str, str]] | None = None,
    default_list_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``board`` archetype payload for the **Can** page (#766).

    The Can is the backlog: every task **without** a due date, across the same enabled
    lists the board aggregates (each card keeps its list/category badge), in one flat
    column. It takes the same full fetched task list as :func:`build_tasks_board` and
    keeps the complement — the two pages partition the tasks, so a task lives on exactly
    one of them and moves between them purely by gaining or losing a due date.

    Cards are the ordinary task cards plus a leading **Schedule** action (a due-only
    ``tasks_update`` form, prefilled to *today*) that places the task on the board; the
    board-level **Add** creates a task with *no* due field offered (and no ``repeat`` —
    a rule needs a due date to anchor it), so new entries land here by construction.
    The only view control is *Show* (open / completed / all — completed undated tasks
    stay reachable, per the page's own filter); grouping would be noise in one column.
    When *lists* is given the Add form gains the same list picker as the board, and with
    two or more lists each card's Edit form gains the move picker (ADR-0038). Tags follow
    the board's honesty rule (#763): the Add offers the chips field (with the known-tags
    typeahead) only when the add targets the local store — with *lists* given every add
    lands on Google, which silently drops tags — and each card's Edit offers it only for
    local tasks.
    """
    tag_suggestions = _distinct_tags(tasks)  # full set (#763): board + Can suggest the union
    undated = [t for t in tasks if not t.due]
    move_lists = lists if lists and len(lists) >= 2 else None
    cards = [
        _task_card(
            t, today=today, move_lists=move_lists, schedule=True, tag_suggestions=tag_suggestions
        )
        for t in undated
    ]
    columns = [{"id": _slug(_CAN_COLUMN), "title": _CAN_COLUMN, "cards": cards}] if cards else []
    add_action: dict[str, Any] = {
        "tool": "tasks_add",
        "label": "Add task",
        "intent": "primary",
        "icon": "plus",
        "icon_only": True,
        "form": True,
        "fields": ["title", "notes", "priority", "tags"],
        "field_options": _TASK_FIELD_OPTIONS,
    }
    if tag_suggestions:
        add_action["field_suggestions"] = {"tags": tag_suggestions}
    if lists:
        # External (Google) target — no tags field, same honesty rule as the board (#763).
        add_action["fields"] = ["title", "list_id", "notes", "priority"]
        add_action.pop("field_suggestions", None)
        add_action["field_choices"] = {
            "list_id": [{"value": list_id, "label": title} for list_id, title in lists],
        }
        if default_list_id is not None:
            add_action["form_values"] = {"list_id": default_list_id}
    return {
        "title": "Can",
        "columns": columns,
        "controls": [_show_control(scope)],
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
    if task.repeat:
        details.append(HoverCardDetail(label="Repeat", value=_repeat_label(task.repeat)))
    # The link leads to the page the task actually lives on (#766): the board for a dated
    # task, the Can for an undated one — "appears in the Can and nowhere else" includes
    # where we send the operator looking for it.
    page_id = TASKS_PAGE_ID if task.due else CAN_PAGE_ID
    return HoverCard(
        title=task.title,
        description=task.notes or "",
        details=details,
        # A calendar-feed chip's hover-card needs a way back to the task (#469's own
        # acceptance criteria); every task hover-card gains it, not just those reached
        # from the calendar — consistent regardless of where the chip was clicked.
        href=HoverCardLink(label="Open in Tasks", url=f"/m/{MODULE_NAME}/{page_id}"),
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
    if task.repeat:
        lines.append(_repeat_summary(task.repeat))
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
