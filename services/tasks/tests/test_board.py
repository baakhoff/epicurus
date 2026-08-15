"""Tests for build_tasks_board / build_tasks_backlog — the `board` archetype payloads
(ADR-0018 / ADR-0049 / #766 / #820).

Pure and deterministic given ``today``, so the grouping, the dated/undated partition,
view controls, and filter echo are exercised here without a database or a clock.
"""

from __future__ import annotations

from typing import Any

from epicurus_tasks.models import Task
from epicurus_tasks.service import (
    BACKLOG_SHOW,
    TASKS_PAGE_ID,
    build_tasks_backlog,
    build_tasks_board,
    calendar_feed_items,
    coerce_group,
    coerce_scope,
    coerce_show,
    coerce_view,
)

TODAY = "2026-06-14"


def _task(
    task_id: str,
    title: str,
    *,
    due: str | None = None,
    notes: str | None = None,
    status: str = "open",
    priority: str | None = None,
    tags: list[str] | None = None,
    list_id: str | None = None,
    list_title: str | None = None,
    repeat: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        due=due,
        notes=notes,
        status=status,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        tags=tags or [],
        list_id=list_id,
        list_title=list_title,
        repeat=repeat,
    )


def test_page_id_is_board() -> None:
    assert TASKS_PAGE_ID == "board"
    assert BACKLOG_SHOW == "backlog"


def test_groups_open_tasks_by_due_bucket() -> None:
    tasks = [
        _task("1", "Overdue thing", due="2026-06-01"),
        _task("2", "Today thing", due="2026-06-14"),
        _task("3", "Future thing", due="2026-12-25"),
        _task("4", "Someday thing"),  # undated → the backlog, never the board (#766)
    ]
    board = build_tasks_board(tasks, today=TODAY)

    # Columns appear in canonical order, empty ones dropped — and there is no
    # "No date" bucket any more: the undated task belongs to the backlog (#766).
    assert [c["title"] for c in board["columns"]] == ["Overdue", "Today", "Upcoming"]
    by_title = {c["title"]: c for c in board["columns"]}
    assert by_title["Overdue"]["cards"][0]["title"] == "Overdue thing"
    assert by_title["Today"]["cards"][0]["title"] == "Today thing"
    assert by_title["Upcoming"]["cards"][0]["title"] == "Future thing"


def test_empty_columns_are_dropped() -> None:
    board = build_tasks_board([_task("1", "x", due=TODAY)], today=TODAY)
    assert [c["title"] for c in board["columns"]] == ["Today"]


# ── Recurrence (#471, ADR-0082) ───────────────────────────────────────────────


def test_add_form_offers_the_repeat_field() -> None:
    board = build_tasks_board([_task("1", "x", due=TODAY)], today=TODAY)
    add = next(a for a in board["actions"] if a["tool"] == "tasks_add")
    assert "repeat" in add["fields"]


def test_recurring_card_shows_a_repeat_badge_and_prefills_edit() -> None:
    board = build_tasks_board(
        [_task("1", "Water plants", due=TODAY, repeat="FREQ=WEEKLY")], today=TODAY
    )
    card = board["columns"][0]["cards"][0]
    assert "Repeats weekly" in [b["label"] for b in card["badges"]]
    edit = next(a for a in card["actions"] if a["tool"] == "tasks_update" and a.get("form"))
    assert "repeat" in edit["fields"]
    assert edit["form_values"]["repeat"] == "FREQ=WEEKLY"


def test_weekdays_rule_reads_as_on_weekdays() -> None:
    board = build_tasks_board(
        [_task("1", "Standup", due=TODAY, repeat="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")], today=TODAY
    )
    card = board["columns"][0]["cards"][0]
    assert "Repeats on weekdays" in [b["label"] for b in card["badges"]]


def test_card_carries_complete_and_edit_actions() -> None:
    board = build_tasks_board(
        [_task("t1", "Buy milk", due="2026-06-20", notes="2 litres")], today=TODAY
    )
    card = board["columns"][0]["cards"][0]
    assert card["id"] == "t1"
    assert card["subtitle"] == "2 litres"

    tools = [a["tool"] for a in card["actions"]]
    assert tools == ["tasks_complete", "tasks_update", "tasks_delete"]

    complete, edit, delete = card["actions"]
    # Each card carries its list_id so a mutation routes to the owning list (ADR-0036).
    assert complete["args"] == {"task_id": "t1", "list_id": None}
    assert "form" not in complete  # one-tap, no form

    assert edit["form"] is True
    assert edit["args"] == {"task_id": "t1", "list_id": None}
    assert edit["fields"] == ["title", "notes", "due", "priority", "tags", "status", "repeat"]
    assert edit["field_options"]["priority"] == ["low", "medium", "high"]
    assert edit["field_options"]["status"] == ["open", "in_progress", "done"]
    assert edit["form_values"]["title"] == "Buy milk"
    assert edit["form_values"]["notes"] == "2 litres"
    assert edit["form_values"]["status"] == "open"

    # Delete (#336) is destructive: danger intent + a confirm prompt (the shell gates it),
    # routing the owning list through the same tasks_delete tool the agent can call.
    assert delete["tool"] == "tasks_delete"
    assert delete["intent"] == "danger"
    assert delete["confirm"]
    assert delete["args"] == {"task_id": "t1", "list_id": None}


def test_due_badge_tone_tracks_bucket() -> None:
    overdue = build_tasks_board([_task("a", "late", due="2020-01-01")], today=TODAY)
    today = build_tasks_board([_task("b", "now", due=TODAY)], today=TODAY)
    upcoming = build_tasks_board([_task("c", "soon", due="2030-01-01")], today=TODAY)

    assert overdue["columns"][0]["cards"][0]["badges"][0]["tone"] == "danger"
    assert today["columns"][0]["cards"][0]["badges"][0]["tone"] == "accent"
    assert upcoming["columns"][0]["cards"][0]["badges"][0]["tone"] == "dim"


def test_no_due_card_has_no_badge() -> None:
    # An undated task lives in the backlog (#766); with no due there is no due badge.
    backlog = build_tasks_backlog([_task("a", "whenever")], today=TODAY)
    assert backlog["columns"][0]["cards"][0]["badges"] == []


def test_priority_badge_added_and_toned() -> None:
    high = Task(id="h", title="Urgent", priority="high")
    med = Task(id="m", title="Moderate", priority="medium")
    low = Task(id="l", title="Someday", priority="low")
    # Undated → the backlog (#766); the badge logic is the shared card builder either way.
    backlog_h = build_tasks_backlog([high], today=TODAY)
    backlog_m = build_tasks_backlog([med], today=TODAY)
    backlog_l = build_tasks_backlog([low], today=TODAY)

    badges_h = backlog_h["columns"][0]["cards"][0]["badges"]
    badges_m = backlog_m["columns"][0]["cards"][0]["badges"]
    badges_l = backlog_l["columns"][0]["cards"][0]["badges"]

    assert badges_h == [{"label": "High", "tone": "danger"}]
    assert badges_m == [{"label": "Medium", "tone": "warn"}]
    assert badges_l == [{"label": "Low", "tone": "dim"}]


def test_tags_rendered_as_accent_badges() -> None:
    task = Task(id="t", title="Tagged", due=TODAY, tags=["work", "q3"])
    board = build_tasks_board([task], today=TODAY)
    badges = board["columns"][0]["cards"][0]["badges"]
    assert {"label": "work", "tone": "accent"} in badges
    assert {"label": "q3", "tone": "accent"} in badges


def test_board_offers_add_action_even_when_empty() -> None:
    board = build_tasks_board([], today=TODAY)
    assert board["title"] == "Tasks"
    assert board["columns"] == []
    add = board["actions"][0]
    assert add["tool"] == "tasks_add"
    assert add["intent"] == "primary"
    # The Add affordance is a compact icon-only "+" (#337).
    assert add["icon_only"] is True
    assert add["form"] is True
    assert add["fields"] == ["title", "notes", "due", "priority", "tags", "repeat"]
    assert add["field_options"]["priority"] == ["low", "medium", "high"]


def test_card_has_category_badge_from_list_title() -> None:
    task = _task("t", "Categorised", due=TODAY, list_id="work", list_title="Work")
    board = build_tasks_board([task], today=TODAY)
    badges = board["columns"][0]["cards"][0]["badges"]
    assert {"label": "Work", "tone": "dim"} in badges


def test_card_has_no_category_badge_without_list_title() -> None:
    board = build_tasks_board([_task("t", "Uncategorised", due=TODAY)], today=TODAY)
    # The due badge is the only one — no category badge without a list title.
    assert board["columns"][0]["cards"][0]["badges"] == [{"label": TODAY, "tone": "accent"}]


def test_add_action_has_list_selector_when_lists_given() -> None:
    board = build_tasks_board(
        [],
        today=TODAY,
        lists=[("@default", "My Tasks"), ("work", "Work")],
        default_list_id="work",
    )
    add = board["actions"][0]
    # No tags field here (#763): the picker offers external (Google) lists only, where a
    # tag would be silently dropped — the form must not pretend to save one.
    assert add["fields"] == ["title", "list_id", "notes", "due", "priority", "repeat"]
    # the list picker is a labeled `field_choices` (value=list id, label=title)
    assert add["field_choices"]["list_id"] == [
        {"value": "@default", "label": "My Tasks"},
        {"value": "work", "label": "Work"},
    ]
    assert add["form_values"]["list_id"] == "work"
    # the plain enum fields stay as `field_options`
    assert add["field_options"]["priority"] == ["low", "medium", "high"]


def test_add_action_has_no_list_selector_when_no_lists() -> None:
    board = build_tasks_board([], today=TODAY, lists=[])
    add = board["actions"][0]
    assert "list_id" not in add["fields"]
    assert "field_choices" not in add


# ── "New list" board action (#474) ─────────────────────────────────────────────


def test_board_offers_new_list_action_when_lists_given() -> None:
    board = build_tasks_board([], today=TODAY, lists=[("@default", "My Tasks")])
    tools = {a["tool"] for a in board["actions"]}
    assert "tasks_create_list" in tools
    new_list = next(a for a in board["actions"] if a["tool"] == "tasks_create_list")
    assert new_list["form"] is True
    assert new_list["fields"] == ["title"]


def test_board_omits_new_list_action_without_lists() -> None:
    # No lists means no external account is connected — nothing to create against yet.
    board = build_tasks_board([], today=TODAY, lists=[])
    tools = {a["tool"] for a in board["actions"]}
    assert "tasks_create_list" not in tools
    assert board["actions"] == [board["actions"][0]]  # just "Add task"


def test_edit_action_has_move_picker_when_multiple_lists() -> None:
    # With ≥2 lists each task's Edit form gains a List picker to move it (ADR-0038).
    task = _task("t1", "Movable", due=TODAY, list_id="work", list_title="Work")
    board = build_tasks_board(
        [task],
        today=TODAY,
        lists=[("@default", "My Tasks"), ("work", "Work")],
        default_list_id="work",
    )
    edit = board["columns"][0]["cards"][0]["actions"][1]
    assert "to_list_id" in edit["fields"]
    assert edit["field_choices"]["to_list_id"] == [
        {"value": "@default", "label": "My Tasks"},
        {"value": "work", "label": "Work"},
    ]
    # Prefilled to the task's current list, so leaving it unchanged is a no-op move.
    assert edit["form_values"]["to_list_id"] == "work"
    # The source list is still carried in args for routing.
    assert edit["args"] == {"task_id": "t1", "list_id": "work"}


def test_edit_action_has_no_move_picker_with_a_single_list() -> None:
    # One list → nowhere to move to → no move picker (the Add picker can still show).
    task = _task("t1", "Stuck", due=TODAY, list_id="work", list_title="Work")
    board = build_tasks_board([task], today=TODAY, lists=[("work", "Work")], default_list_id="work")
    edit = board["columns"][0]["cards"][0]["actions"][1]
    assert "to_list_id" not in edit["fields"]
    assert "field_choices" not in edit


# ── view controls (ADR-0049 / #767) ───────────────────────────────────────────


def test_board_declares_view_group_and_show_controls() -> None:
    board = build_tasks_board([], today=TODAY)
    controls = {c["id"]: c for c in board["controls"]}
    assert set(controls) == {"view", "group", "show"}
    # Declaration order is toolbar order: the representation switch leads (#767).
    assert [c["id"] for c in board["controls"]] == ["view", "group", "show"]
    assert controls["view"]["label"] == "View"
    assert controls["view"]["value"] == "board"
    assert [o["value"] for o in controls["view"]["options"]] == ["board", "list", "calendar"]
    assert controls["group"]["label"] == "Group by"
    assert controls["group"]["value"] == "due"
    group_values = [o["value"] for o in controls["group"]["options"]]
    assert group_values == ["due", "status", "priority", "none"]
    assert controls["show"]["label"] == "Show"
    assert controls["show"]["value"] == "open"
    # Backlog is a fourth Show option (#820), appended after the three status scopes.
    assert [o["value"] for o in controls["show"]["options"]] == [
        "open",
        "done",
        "all",
        "backlog",
    ]


def test_group_control_offers_list_option_only_with_lists() -> None:
    no_lists = build_tasks_board([], today=TODAY)
    group = next(c for c in no_lists["controls"] if c["id"] == "group")
    assert "list" not in [o["value"] for o in group["options"]]

    with_lists = build_tasks_board([], today=TODAY, lists=[("work", "Work"), ("home", "Home")])
    group2 = next(c for c in with_lists["controls"] if c["id"] == "group")
    # "List" is spliced in before the flat "None" option.
    assert [o["value"] for o in group2["options"]] == ["due", "status", "priority", "list", "none"]


def test_controls_echo_active_selection() -> None:
    board = build_tasks_board([], today=TODAY, group_by="priority", scope="all")
    values = {c["id"]: c["value"] for c in board["controls"]}
    assert values == {"view": "board", "group": "priority", "show": "all"}


def test_list_and_calendar_views_hide_the_group_control() -> None:
    # Grouping shapes kanban columns; the List/Calendar representations are flat/date-keyed,
    # so offering Group by there would be a dead knob (#767). Show applies under every view.
    for view in ("list", "calendar"):
        board = build_tasks_board([], today=TODAY, view=view, scope="done")
        values = {c["id"]: c["value"] for c in board["controls"]}
        assert values == {"view": view, "show": "done"}, view


def test_calendar_view_drops_backlog_from_the_show_options() -> None:
    # A backlog has no due dates to place on a calendar grid, so Calendar drops the option
    # from Show entirely (#820) — the same dead-knob treatment #767 gives Group by under
    # List/Calendar. Board and List keep all four options.
    for view in ("board", "list"):
        board = build_tasks_board([], today=TODAY, view=view)
        show = next(c for c in board["controls"] if c["id"] == "show")
        assert "backlog" in [o["value"] for o in show["options"]], view

    calendar = build_tasks_board([], today=TODAY, view="calendar")
    show = next(c for c in calendar["controls"] if c["id"] == "show")
    assert [o["value"] for o in show["options"]] == ["open", "done", "all"]


def test_view_does_not_change_the_columns_payload() -> None:
    # The three representations render the *same fetched data* (#767): the module's columns
    # and cards are identical across views — only the controls echo differs.
    tasks = [_task("1", "a", due=TODAY), _task("2", "b", due="2026-06-20")]
    by_view = {
        view: build_tasks_board(tasks, today=TODAY, view=view)
        for view in ("board", "list", "calendar")
    }
    assert by_view["board"]["columns"] == by_view["list"]["columns"]
    assert by_view["board"]["columns"] == by_view["calendar"]["columns"]
    assert by_view["board"]["actions"] == by_view["list"]["actions"]


def test_coerce_view_clamps_unknown_to_board() -> None:
    assert coerce_view("list") == "list"
    assert coerce_view("calendar") == "calendar"
    assert coerce_view("hologram") == "board"
    assert coerce_view(None) == "board"


def test_cards_carry_structured_fields_for_alternate_views() -> None:
    # Data, not presentation (#767): the List view sorts by these and the Calendar places
    # by `due` — rendered badge strings can't support either. Additive to the card shape.
    task = _task(
        "t1",
        "Structured",
        due="2026-06-20T00:00:00.000Z",  # a provider may store a full RFC 3339 stamp
        priority="high",
        tags=["deep", "q3"],
        list_id="work",
        list_title="Work",
    )
    board = build_tasks_board([task], today=TODAY)
    card = board["columns"][0]["cards"][0]
    assert card["due"] == "2026-06-20"  # sliced to the bare ISO date
    assert card["priority"] == "high"
    assert card["tags"] == ["deep", "q3"]
    assert card["list_title"] == "Work"


def test_structured_fields_default_to_none_or_empty() -> None:
    backlog = build_tasks_backlog([_task("t1", "Bare")], today=TODAY)
    card = backlog["columns"][0]["cards"][0]
    assert card["due"] is None
    assert card["priority"] is None
    assert card["tags"] == []
    assert card["list_title"] is None


def test_backlog_declares_view_and_show_but_no_group() -> None:
    # Folded onto the Tasks page in #820: the backlog keeps the *View* switcher (Board/List
    # both render it sensibly — nothing dated to place on a Calendar, but that combination
    # never reaches here, see coerce_show) — it just never offers *Group by* (a flat backlog
    # plus its muted Completed section has nothing to group, #767's dead-knob rule).
    backlog = build_tasks_backlog([], today=TODAY)
    assert [c["id"] for c in backlog["controls"]] == ["view", "show"]
    controls = {c["id"]: c for c in backlog["controls"]}
    assert controls["view"]["value"] == "board"
    assert controls["show"]["value"] == "backlog"
    assert [o["value"] for o in controls["show"]["options"]] == [
        "open",
        "done",
        "all",
        "backlog",
    ]


def test_backlog_echoes_the_list_view() -> None:
    backlog = build_tasks_backlog([], today=TODAY, view="list")
    view = next(c for c in backlog["controls"] if c["id"] == "view")
    assert view["value"] == "list"
    assert "group" not in {c["id"] for c in backlog["controls"]}


# ── grouping strategies (ADR-0049) ────────────────────────────────────────────


def test_group_by_status_columns_in_order() -> None:
    tasks = [
        _task("o", "Open one", due=TODAY),
        _task("p", "Doing", due=TODAY, status="in_progress"),
        _task("d", "Done one", due=TODAY, status="done"),
    ]
    board = build_tasks_board(tasks, today=TODAY, group_by="status", scope="all")
    cols = {c["title"]: [card["title"] for card in c["cards"]] for c in board["columns"]}
    assert list(cols.keys()) == ["Open", "In progress", "Completed"]
    assert cols["Open"] == ["Open one"]
    assert cols["In progress"] == ["Doing"]
    assert cols["Completed"] == ["Done one"]


def test_group_by_priority_orders_high_to_none() -> None:
    tasks = [
        _task("1", "hi", due=TODAY, priority="high"),
        _task("2", "lo", due=TODAY, priority="low"),
        _task("3", "none", due=TODAY),
    ]
    board = build_tasks_board(tasks, today=TODAY, group_by="priority")
    assert [c["title"] for c in board["columns"]] == ["High", "Low", "No priority"]


def test_group_by_none_is_a_single_flat_column() -> None:
    tasks = [_task("1", "a", due="2026-06-01"), _task("2", "b", due=TODAY)]
    board = build_tasks_board(tasks, today=TODAY, group_by="none")
    assert [c["title"] for c in board["columns"]] == ["All tasks"]
    assert len(board["columns"][0]["cards"]) == 2


def test_group_by_list_orders_by_lists_then_extras() -> None:
    tasks = [
        _task("1", "w", due=TODAY, list_id="work", list_title="Work"),
        _task("2", "h", due=TODAY, list_id="home", list_title="Home"),
        _task("3", "p", due=TODAY),  # local default → "Personal" fallback (no list_title)
    ]
    board = build_tasks_board(
        tasks, today=TODAY, group_by="list", lists=[("work", "Work"), ("home", "Home")]
    )
    assert [c["title"] for c in board["columns"]] == ["Work", "Home", "Personal"]


def test_group_by_list_without_lists_falls_back_to_due() -> None:
    board = build_tasks_board([_task("1", "x", due="2026-06-01")], today=TODAY, group_by="list")
    assert [c["title"] for c in board["columns"]] == ["Overdue"]
    group = next(c for c in board["controls"] if c["id"] == "group")
    assert group["value"] == "due"  # control echoes the corrected grouping


def test_due_badge_tone_is_independent_of_grouping() -> None:
    # Even grouped by priority, an overdue task's due badge stays "danger".
    overdue = _task("a", "late", due="2020-01-01", priority="low")
    board = build_tasks_board([overdue], today=TODAY, group_by="priority")
    due_badge = board["columns"][0]["cards"][0]["badges"][0]
    assert due_badge == {"label": "2020-01-01", "tone": "danger"}


# ── completed cards: done flag + Reopen (ADR-0049) ────────────────────────────


def test_completed_card_is_done_and_offers_reopen() -> None:
    board = build_tasks_board(
        [_task("d", "Finished", due=TODAY, status="done")],
        today=TODAY,
        group_by="status",
        scope="done",
    )
    card = board["columns"][0]["cards"][0]
    assert card["done"] is True
    primary = card["actions"][0]
    assert primary["tool"] == "tasks_update"
    assert primary["label"] == "Reopen"
    assert primary["args"]["status"] == "open"
    assert primary["args"]["task_id"] == "d"


def test_open_card_is_not_done_and_offers_complete() -> None:
    board = build_tasks_board([_task("o", "Open", due=TODAY)], today=TODAY)
    card = board["columns"][0]["cards"][0]
    assert card["done"] is False
    primary = card["actions"][0]
    assert primary["tool"] == "tasks_complete"
    assert primary["label"] == "Complete"


# ── the backlog: the undated partition, folded onto Show (#766 / #820) ────────


def _card_ids(page: dict[str, Any]) -> list[str]:
    """Every card id on a built page, in column order."""
    return [card["id"] for column in page["columns"] for card in column["cards"]]


def test_board_excludes_undated_tasks_under_every_grouping() -> None:
    tasks = [
        _task("dated", "Scheduled", due=TODAY, list_id="work", list_title="Work"),
        _task("undated", "Someday", list_id="work", list_title="Work"),
    ]
    for group in ("due", "status", "priority", "list", "none"):
        board = build_tasks_board(
            tasks, today=TODAY, group_by=group, scope="all", lists=[("work", "Work")]
        )
        assert _card_ids(board) == ["dated"], f"group={group!r} leaked the undated task"
        assert "No date" not in [c["title"] for c in board["columns"]]


def test_backlog_and_board_partition_the_same_fetch() -> None:
    # Both builders take the same full task list; every task lands behind exactly one
    # Show value.
    tasks = [
        _task("a", "Scheduled", due="2026-06-01"),
        _task("b", "Backlog one"),
        _task("c", "Backlog two"),
    ]
    board_ids = set(_card_ids(build_tasks_board(tasks, today=TODAY)))
    backlog_ids = set(_card_ids(build_tasks_backlog(tasks, today=TODAY)))
    assert board_ids == {"a"}
    assert backlog_ids == {"b", "c"}
    assert board_ids | backlog_ids == {"a", "b", "c"}
    assert board_ids & backlog_ids == set()


def test_backlog_is_a_single_flat_column_when_all_open() -> None:
    backlog = build_tasks_backlog([_task("1", "x"), _task("2", "y")], today=TODAY)
    # The page heading stays "Tasks" — Backlog is a Show option on the same page, not a
    # separately titled page any more (#820).
    assert backlog["title"] == "Tasks"
    assert [c["title"] for c in backlog["columns"]] == ["Backlog"]
    assert len(backlog["columns"][0]["cards"]) == 2


def test_backlog_with_no_undated_tasks_has_no_columns() -> None:
    backlog = build_tasks_backlog([_task("1", "Scheduled", due=TODAY)], today=TODAY)
    assert backlog["columns"] == []


def test_backlog_card_leads_with_a_schedule_action() -> None:
    backlog = build_tasks_backlog(
        [_task("t1", "Someday", list_id="work", list_title="Work")], today=TODAY
    )
    card = backlog["columns"][0]["cards"][0]
    tools = [a["tool"] for a in card["actions"]]
    assert tools == ["tasks_update", "tasks_complete", "tasks_update", "tasks_delete"]
    schedule = card["actions"][0]
    assert schedule["label"] == "Schedule"
    assert schedule["form"] is True
    # A due-only form — the existing SchemaForm date picker — prefilled to today so the
    # one-tap path (open, submit) schedules for today; the mutation routes to the owning list.
    assert schedule["fields"] == ["due"]
    assert schedule["form_values"] == {"due": TODAY}
    assert schedule["args"] == {"task_id": "t1", "list_id": "work"}


def test_board_cards_have_no_schedule_action() -> None:
    board = build_tasks_board([_task("t1", "Scheduled", due=TODAY)], today=TODAY)
    tools = [a["tool"] for a in board["columns"][0]["cards"][0]["actions"]]
    assert tools == ["tasks_complete", "tasks_update", "tasks_delete"]


def test_backlog_add_offers_no_due_or_repeat_field() -> None:
    backlog = build_tasks_backlog([], today=TODAY)
    add = next(a for a in backlog["actions"] if a["tool"] == "tasks_add")
    # The backlog's Add creates an undated task by construction: no due to fill in, and no
    # repeat either (a rule needs a due date to anchor it).
    assert add["fields"] == ["title", "notes", "priority", "tags"]
    assert add["form"] is True
    assert add["field_options"]["priority"] == ["low", "medium", "high"]


def test_backlog_add_offers_list_picker_when_lists_given() -> None:
    backlog = build_tasks_backlog(
        [],
        today=TODAY,
        lists=[("@default", "My Tasks"), ("work", "Work")],
        default_list_id="work",
    )
    add = backlog["actions"][0]
    # No tags field with a (Google-only) list picker — same honesty rule as the board (#763).
    assert add["fields"] == ["title", "list_id", "notes", "priority"]
    assert add["field_choices"]["list_id"] == [
        {"value": "@default", "label": "My Tasks"},
        {"value": "work", "label": "Work"},
    ]
    assert add["form_values"]["list_id"] == "work"
    # "New list" stays a board-view affordance — the backlog keeps a single Add action.
    assert [a["tool"] for a in backlog["actions"]] == ["tasks_add"]


# ── the axis nuance (#820): Show used to be status scope *and* the Can's own filter;
# folded onto one control, a completed undated task stays reachable by splitting the
# backlog itself into a Backlog (open/in-progress) column and a muted Completed one,
# rather than by a second, independent status filter. Pinning this is the acceptance
# bar: "no undated task, open or completed, becomes unreachable".


def test_backlog_splits_open_and_completed_undated_into_two_columns() -> None:
    tasks = [
        _task("open1", "Still open"),
        _task("doing", "In progress", status="in_progress"),
        _task("done1", "Finished", status="done"),
    ]
    backlog = build_tasks_backlog(tasks, today=TODAY)
    assert [c["title"] for c in backlog["columns"]] == ["Backlog", "Completed"]
    cols = {c["title"]: [card["id"] for card in c["cards"]] for c in backlog["columns"]}
    # "open" scope's own convention (ADR-0049's TaskScope) includes in-progress alongside
    # open — the Backlog column follows the same rule, only "done" moves to Completed.
    assert cols["Backlog"] == ["open1", "doing"]
    assert cols["Completed"] == ["done1"]


def test_backlog_completed_only_yields_just_the_completed_column() -> None:
    # No undated task is unreachable, open or completed (#820's acceptance bar): an
    # all-completed backlog still renders — as a lone Completed column, not an empty page.
    backlog = build_tasks_backlog([_task("d", "Done backlog", status="done")], today=TODAY)
    assert [c["title"] for c in backlog["columns"]] == ["Completed"]
    assert _card_ids(backlog) == ["d"]


def test_backlog_completed_card_is_done_and_offers_reopen() -> None:
    backlog = build_tasks_backlog([_task("d", "Done backlog", status="done")], today=TODAY)
    card = backlog["columns"][0]["cards"][0]
    assert card["done"] is True
    # Schedule still leads; the primary complete/reopen slot follows it — the muted
    # Completed section is an ordinary card set, not a stripped-down view.
    reopen = card["actions"][1]
    assert reopen["label"] == "Reopen"
    assert reopen["args"]["status"] == "open"


def test_backlog_keeps_list_badge_and_move_picker() -> None:
    task = _task("t1", "Someday", list_id="work", list_title="Work")
    backlog = build_tasks_backlog(
        [task], today=TODAY, lists=[("@default", "My Tasks"), ("work", "Work")]
    )
    card = backlog["columns"][0]["cards"][0]
    assert {"label": "Work", "tone": "dim"} in card["badges"]  # category badge preserved
    edit = next(a for a in card["actions"] if a.get("form") and "title" in (a["fields"] or []))
    assert "to_list_id" in edit["fields"]  # the ADR-0038 move picker, same as the board


def test_schedule_round_trip_moves_a_task_between_backlog_and_board() -> None:
    # Scheduling = giving the task a due date; clearing it sends the task back. The Show
    # partition is on `due` alone, so the round trip is expressible purely through the
    # builders.
    undated = _task("t", "Buy a drill")
    assert _card_ids(build_tasks_backlog([undated], today=TODAY)) == ["t"]
    assert build_tasks_board([undated], today=TODAY)["columns"] == []

    scheduled = undated.model_copy(update={"due": "2026-06-20"})
    assert build_tasks_backlog([scheduled], today=TODAY)["columns"] == []
    assert _card_ids(build_tasks_board([scheduled], today=TODAY)) == ["t"]

    cleared = scheduled.model_copy(update={"due": None})
    assert build_tasks_board([cleared], today=TODAY)["columns"] == []
    assert _card_ids(build_tasks_backlog([cleared], today=TODAY)) == ["t"]


# ── tags: grouping, suggestions, and Google honesty (#763) ────────────────────


def test_group_by_tags_is_multi_membership_with_untagged_last() -> None:
    tasks = [
        _task("two", "Two tags", due=TODAY, tags=["work", "errand"]),
        _task("one", "One tag", due=TODAY, tags=["work"]),
        _task("bare", "No tags", due=TODAY),
    ]
    board = build_tasks_board(tasks, today=TODAY, group_by="tags")
    cols = {c["title"]: [card["id"] for card in c["cards"]] for c in board["columns"]}
    # Alphabetical column order, Untagged closing the board.
    assert list(cols.keys()) == ["errand", "work", "Untagged"]
    # A task with two tags appears under both; untagged tasks land in Untagged.
    assert cols["errand"] == ["two"]
    assert cols["work"] == ["two", "one"]
    assert cols["Untagged"] == ["bare"]


def test_tags_columns_sort_case_insensitively_and_stay_stable() -> None:
    tasks = [
        _task("1", "a", due=TODAY, tags=["Zoo"]),
        _task("2", "b", due=TODAY, tags=["api"]),
        _task("3", "c", due=TODAY, tags=["Beta"]),
    ]
    first = build_tasks_board(tasks, today=TODAY, group_by="tags")
    assert [c["title"] for c in first["columns"]] == ["api", "Beta", "Zoo"]
    # Deterministic across reloads: the same input yields the same column order.
    again = build_tasks_board(list(reversed(tasks)), today=TODAY, group_by="tags")
    assert [c["title"] for c in again["columns"]] == ["api", "Beta", "Zoo"]


def test_tags_group_option_offered_only_when_a_visible_task_has_tags() -> None:
    untagged = build_tasks_board([_task("1", "x", due=TODAY)], today=TODAY)
    group = next(c for c in untagged["controls"] if c["id"] == "group")
    assert "tags" not in [o["value"] for o in group["options"]]

    tagged = build_tasks_board([_task("1", "x", due=TODAY, tags=["work"])], today=TODAY)
    group2 = next(c for c in tagged["controls"] if c["id"] == "group")
    assert [o["value"] for o in group2["options"]] == ["due", "status", "priority", "tags", "none"]

    # A tag on an *invisible* (undated → backlog) task doesn't make the board offer the
    # grouping — there would be nothing to group.
    backlog_only = build_tasks_board([_task("1", "x", tags=["work"])], today=TODAY)
    group3 = next(c for c in backlog_only["controls"] if c["id"] == "group")
    assert "tags" not in [o["value"] for o in group3["options"]]


def test_group_options_order_with_lists_and_tags() -> None:
    board = build_tasks_board(
        [_task("1", "x", due=TODAY, tags=["work"])],
        today=TODAY,
        lists=[("work", "Work")],
    )
    group = next(c for c in board["controls"] if c["id"] == "group")
    assert [o["value"] for o in group["options"]] == [
        "due",
        "status",
        "priority",
        "list",
        "tags",
        "none",
    ]


def test_group_by_tags_without_tags_falls_back_to_due() -> None:
    board = build_tasks_board([_task("1", "x", due="2026-06-01")], today=TODAY, group_by="tags")
    assert [c["title"] for c in board["columns"]] == ["Overdue"]
    group = next(c for c in board["controls"] if c["id"] == "group")
    assert group["value"] == "due"  # control echoes the corrected grouping


def test_add_and_edit_carry_known_tags_as_suggestions() -> None:
    # The module supplies the distinct tags as field metadata; the shell renders the
    # typeahead (ADR-0018/0019). Alphabetical, case-insensitive, deduped.
    tasks = [
        _task("1", "a", due=TODAY, tags=["Work", "errand"]),
        _task("2", "b", due=TODAY, tags=["work", "Alpha"]),
    ]
    board = build_tasks_board(tasks, today=TODAY)
    add = next(a for a in board["actions"] if a["tool"] == "tasks_add")
    assert add["field_suggestions"] == {"tags": ["Alpha", "errand", "Work"]}
    edit = board["columns"][0]["cards"][0]["actions"][1]
    assert edit["field_suggestions"] == {"tags": ["Alpha", "errand", "Work"]}


def test_tag_suggestions_span_board_and_backlog() -> None:
    # A tag used only on a backlog (undated) task still autocompletes on the board, and
    # vice versa — both builders compute suggestions over the full fetched set.
    tasks = [
        _task("dated", "On the board", due=TODAY, tags=["planned"]),
        _task("undated", "In the backlog", tags=["someday"]),
    ]
    board_add = next(
        a for a in build_tasks_board(tasks, today=TODAY)["actions"] if a["tool"] == "tasks_add"
    )
    backlog_add = build_tasks_backlog(tasks, today=TODAY)["actions"][0]
    assert board_add["field_suggestions"] == {"tags": ["planned", "someday"]}
    assert backlog_add["field_suggestions"] == {"tags": ["planned", "someday"]}


def test_no_suggestions_key_without_any_tags() -> None:
    board = build_tasks_board([_task("1", "x", due=TODAY)], today=TODAY)
    add = next(a for a in board["actions"] if a["tool"] == "tasks_add")
    assert "field_suggestions" not in add


def test_google_task_edit_hides_the_tags_field() -> None:
    # A Google task's tags would be silently dropped by the provider (no such field) —
    # the Edit form hides the field rather than pretending to save it (#763).
    external = _task("g1", "On Google", due=TODAY, list_id="work", list_title="Work")
    board = build_tasks_board([external], today=TODAY, lists=[("work", "Work")])
    edit = board["columns"][0]["cards"][0]["actions"][1]
    assert edit["tool"] == "tasks_update"
    assert "tags" not in edit["fields"]
    assert "tags" not in edit["form_values"]
    assert "field_suggestions" not in edit


def test_local_task_edit_keeps_the_tags_field() -> None:
    local = _task("l1", "Local", due=TODAY, tags=["work"])
    board = build_tasks_board([local], today=TODAY)
    edit = board["columns"][0]["cards"][0]["actions"][1]
    assert "tags" in edit["fields"]
    assert edit["form_values"]["tags"] == "work"


def test_list_columns_carry_their_list_id_for_drop_targets() -> None:
    # Drag-move drop targets match by column `list_id`, not title (#763): a tag or status
    # column that happens to share a list's name must never accept a list-move drop.
    tasks = [
        _task("1", "w", due=TODAY, list_id="L-work", list_title="Work"),
        _task("2", "p", due=TODAY),  # local → "Personal" fallback, not a droppable list
    ]
    by_list = build_tasks_board(tasks, today=TODAY, group_by="list", lists=[("L-work", "Work")])
    columns = {c["title"]: c for c in by_list["columns"]}
    assert columns["Work"]["list_id"] == "L-work"
    assert "list_id" not in columns["Personal"]

    # Under any other grouping no column is a list — none carries a list_id, so a tag
    # column titled "Work" is not a drop target even though a list shares the name.
    tagged = [_task("t", "tagged like a list", due=TODAY, tags=["Work"])]
    by_tags = build_tasks_board(tagged, today=TODAY, group_by="tags", lists=[("L-work", "Work")])
    assert all("list_id" not in c for c in by_tags["columns"])


# ── query-param coercion (ADR-0049) ───────────────────────────────────────────


def test_coerce_group_clamps_unknown_to_due() -> None:
    assert coerce_group("priority") == "priority"
    assert coerce_group("list") == "list"
    assert coerce_group("tags") == "tags"
    assert coerce_group("nonsense") == "due"
    assert coerce_group(None) == "due"


def test_coerce_scope_clamps_unknown_to_open() -> None:
    assert coerce_scope("all") == "all"
    assert coerce_scope("done") == "done"
    assert coerce_scope("bogus") == "open"
    assert coerce_scope(None) == "open"


def test_coerce_show_accepts_the_three_scopes_and_backlog() -> None:
    # `backlog` is a page-level Show value (#820) — a peer of the three status scopes,
    # not a widened TaskScope; coerce_show clamps all four, unlike coerce_scope's three.
    for view in ("board", "list"):
        assert coerce_show("all", view=view) == "all"
        assert coerce_show("done", view=view) == "done"
        assert coerce_show("backlog", view=view) == "backlog"
    assert coerce_show("bogus", view="board") == "open"
    assert coerce_show(None, view="board") == "open"


def test_coerce_show_corrects_backlog_to_open_under_calendar() -> None:
    # A backlog has nothing dated to place on a calendar grid — an explicit or
    # stale `show=backlog` under `view=calendar` is corrected back to the default (#820),
    # the same dead-knob treatment #767 gives Group by under List/Calendar. Any other Show
    # value is untouched under Calendar.
    assert coerce_show("backlog", view="calendar") == "open"
    assert coerce_show("all", view="calendar") == "all"
    assert coerce_show("done", view="calendar") == "done"
    assert coerce_show(None, view="calendar") == "open"


# ── calendar-feed items (#469) ────────────────────────────────────────────────


def test_calendar_feed_items_includes_a_due_date_in_range() -> None:
    tasks = [_task("1", "In range", due="2026-07-15")]
    items = calendar_feed_items(tasks, "2026-07-01", "2026-08-01")
    assert items == [
        {
            "id": "1",
            "title": "In range",
            "date": "2026-07-15",
            "status": "open",
            "ref_id": "1",
            "kind": "task",
        }
    ]


def test_calendar_feed_items_excludes_dates_outside_the_range() -> None:
    tasks = [
        _task("before", "Too early", due="2026-06-30"),
        _task("after", "Too late (end exclusive)", due="2026-08-01"),
        _task("in", "Just right", due="2026-07-31"),
    ]
    items = calendar_feed_items(tasks, "2026-07-01", "2026-08-01")
    assert [i["id"] for i in items] == ["in"]


def test_calendar_feed_items_excludes_tasks_without_a_due_date() -> None:
    tasks = [_task("1", "Someday", due=None)]
    assert calendar_feed_items(tasks, "2026-07-01", "2026-08-01") == []


def test_calendar_feed_items_reflects_the_tasks_own_status() -> None:
    # "open" scope includes both open and in_progress (ADR-0049's TaskScope) — the feed
    # item's status must say which, not flatten both to a literal "open".
    tasks = [_task("1", "Working on it", due="2026-07-15", status="in_progress")]
    items = calendar_feed_items(tasks, "2026-07-01", "2026-08-01")
    assert items[0]["status"] == "in_progress"


def test_calendar_feed_items_accepts_a_full_datetime_due_string() -> None:
    # `due` may be a full RFC-3339 timestamp on some providers (Task.due's documented
    # shape) — only the date portion anchors the calendar day and the range comparison.
    tasks = [_task("1", "Timestamped", due="2026-07-15T09:00:00.000Z")]
    items = calendar_feed_items(tasks, "2026-07-01", "2026-08-01")
    assert items[0]["date"] == "2026-07-15"
