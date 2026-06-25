"""Tests for build_tasks_board — the `board` archetype payload (ADR-0018 / ADR-0049).

Pure and deterministic given ``today``, so the grouping, view controls, and filter
echo are exercised here without a database or a clock.
"""

from __future__ import annotations

from epicurus_tasks.models import Task
from epicurus_tasks.service import (
    TASKS_PAGE_ID,
    build_tasks_board,
    coerce_group,
    coerce_scope,
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
    list_id: str | None = None,
    list_title: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        due=due,
        notes=notes,
        status=status,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        list_id=list_id,
        list_title=list_title,
    )


def test_page_id_is_board() -> None:
    assert TASKS_PAGE_ID == "board"


def test_groups_open_tasks_by_due_bucket() -> None:
    tasks = [
        _task("1", "Overdue thing", due="2026-06-01"),
        _task("2", "Today thing", due="2026-06-14"),
        _task("3", "Future thing", due="2026-12-25"),
        _task("4", "Someday thing"),
    ]
    board = build_tasks_board(tasks, today=TODAY)

    # Columns appear in canonical order, empty ones dropped.
    assert [c["title"] for c in board["columns"]] == ["Overdue", "Today", "Upcoming", "No date"]
    by_title = {c["title"]: c for c in board["columns"]}
    assert by_title["Overdue"]["cards"][0]["title"] == "Overdue thing"
    assert by_title["Today"]["cards"][0]["title"] == "Today thing"
    assert by_title["Upcoming"]["cards"][0]["title"] == "Future thing"
    assert by_title["No date"]["cards"][0]["title"] == "Someday thing"


def test_empty_columns_are_dropped() -> None:
    board = build_tasks_board([_task("1", "x", due=TODAY)], today=TODAY)
    assert [c["title"] for c in board["columns"]] == ["Today"]


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
    assert edit["fields"] == ["title", "notes", "due", "priority", "tags", "status"]
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
    board = build_tasks_board([_task("a", "whenever")], today=TODAY)
    assert board["columns"][0]["cards"][0]["badges"] == []


def test_priority_badge_added_and_toned() -> None:
    high = Task(id="h", title="Urgent", priority="high")
    med = Task(id="m", title="Moderate", priority="medium")
    low = Task(id="l", title="Someday", priority="low")
    board_h = build_tasks_board([high], today=TODAY)
    board_m = build_tasks_board([med], today=TODAY)
    board_l = build_tasks_board([low], today=TODAY)

    badges_h = board_h["columns"][0]["cards"][0]["badges"]
    badges_m = board_m["columns"][0]["cards"][0]["badges"]
    badges_l = board_l["columns"][0]["cards"][0]["badges"]

    assert badges_h == [{"label": "High", "tone": "danger"}]
    assert badges_m == [{"label": "Medium", "tone": "warn"}]
    assert badges_l == [{"label": "Low", "tone": "dim"}]


def test_tags_rendered_as_accent_badges() -> None:
    task = Task(id="t", title="Tagged", tags=["work", "q3"])
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
    assert add["fields"] == ["title", "notes", "due", "priority", "tags"]
    assert add["field_options"]["priority"] == ["low", "medium", "high"]


def test_card_has_category_badge_from_list_title() -> None:
    task = _task("t", "Categorised", list_id="work", list_title="Work")
    board = build_tasks_board([task], today=TODAY)
    badges = board["columns"][0]["cards"][0]["badges"]
    assert {"label": "Work", "tone": "dim"} in badges


def test_card_has_no_category_badge_without_list_title() -> None:
    board = build_tasks_board([_task("t", "Uncategorised")], today=TODAY)
    assert board["columns"][0]["cards"][0]["badges"] == []


def test_add_action_has_list_selector_when_lists_given() -> None:
    board = build_tasks_board(
        [],
        today=TODAY,
        lists=[("@default", "My Tasks"), ("work", "Work")],
        default_list_id="work",
    )
    add = board["actions"][0]
    assert add["fields"] == ["title", "list_id", "notes", "due", "priority", "tags"]
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


# ── view controls (ADR-0049) ──────────────────────────────────────────────────


def test_board_declares_group_and_show_controls() -> None:
    board = build_tasks_board([], today=TODAY)
    controls = {c["id"]: c for c in board["controls"]}
    assert set(controls) == {"group", "show"}
    assert controls["group"]["label"] == "Group by"
    assert controls["group"]["value"] == "due"
    group_values = [o["value"] for o in controls["group"]["options"]]
    assert group_values == ["due", "status", "priority", "none"]
    assert controls["show"]["label"] == "Show"
    assert controls["show"]["value"] == "open"
    assert [o["value"] for o in controls["show"]["options"]] == ["open", "done", "all"]


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
    assert values == {"group": "priority", "show": "all"}


# ── grouping strategies (ADR-0049) ────────────────────────────────────────────


def test_group_by_status_columns_in_order() -> None:
    tasks = [
        _task("o", "Open one"),
        _task("p", "Doing", status="in_progress"),
        _task("d", "Done one", status="done"),
    ]
    board = build_tasks_board(tasks, today=TODAY, group_by="status", scope="all")
    cols = {c["title"]: [card["title"] for card in c["cards"]] for c in board["columns"]}
    assert list(cols.keys()) == ["Open", "In progress", "Completed"]
    assert cols["Open"] == ["Open one"]
    assert cols["In progress"] == ["Doing"]
    assert cols["Completed"] == ["Done one"]


def test_group_by_priority_orders_high_to_none() -> None:
    tasks = [
        _task("1", "hi", priority="high"),
        _task("2", "lo", priority="low"),
        _task("3", "none"),
    ]
    board = build_tasks_board(tasks, today=TODAY, group_by="priority")
    assert [c["title"] for c in board["columns"]] == ["High", "Low", "No priority"]


def test_group_by_none_is_a_single_flat_column() -> None:
    tasks = [_task("1", "a", due="2026-06-01"), _task("2", "b")]
    board = build_tasks_board(tasks, today=TODAY, group_by="none")
    assert [c["title"] for c in board["columns"]] == ["All tasks"]
    assert len(board["columns"][0]["cards"]) == 2


def test_group_by_list_orders_by_lists_then_extras() -> None:
    tasks = [
        _task("1", "w", list_id="work", list_title="Work"),
        _task("2", "h", list_id="home", list_title="Home"),
        _task("3", "p"),  # local default → "Personal" fallback (no list_title)
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
        [_task("d", "Finished", status="done")], today=TODAY, group_by="status", scope="done"
    )
    card = board["columns"][0]["cards"][0]
    assert card["done"] is True
    primary = card["actions"][0]
    assert primary["tool"] == "tasks_update"
    assert primary["label"] == "Reopen"
    assert primary["args"]["status"] == "open"
    assert primary["args"]["task_id"] == "d"


def test_open_card_is_not_done_and_offers_complete() -> None:
    board = build_tasks_board([_task("o", "Open")], today=TODAY)
    card = board["columns"][0]["cards"][0]
    assert card["done"] is False
    primary = card["actions"][0]
    assert primary["tool"] == "tasks_complete"
    assert primary["label"] == "Complete"


# ── query-param coercion (ADR-0049) ───────────────────────────────────────────


def test_coerce_group_clamps_unknown_to_due() -> None:
    assert coerce_group("priority") == "priority"
    assert coerce_group("list") == "list"
    assert coerce_group("nonsense") == "due"
    assert coerce_group(None) == "due"


def test_coerce_scope_clamps_unknown_to_open() -> None:
    assert coerce_scope("all") == "all"
    assert coerce_scope("done") == "done"
    assert coerce_scope("bogus") == "open"
    assert coerce_scope(None) == "open"
