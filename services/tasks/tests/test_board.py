"""Tests for build_tasks_board — the `board` archetype payload (ADR-0018).

Pure and deterministic given ``today``, so the due-date bucketing is exercised
here without a database or a clock.
"""

from __future__ import annotations

from epicurus_tasks.models import Task
from epicurus_tasks.service import TASKS_PAGE_ID, build_tasks_board

TODAY = "2026-06-14"


def _task(
    task_id: str,
    title: str,
    *,
    due: str | None = None,
    notes: str | None = None,
    list_id: str | None = None,
    list_title: str | None = None,
) -> Task:
    return Task(
        id=task_id, title=title, due=due, notes=notes, list_id=list_id, list_title=list_title
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
    assert tools == ["tasks_complete", "tasks_update"]

    complete, edit = card["actions"]
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
    assert add["field_options"]["list_id"] == [
        {"value": "@default", "label": "My Tasks"},
        {"value": "work", "label": "Work"},
    ]
    assert add["form_values"]["list_id"] == "work"
    # the plain enum fields are untouched
    assert add["field_options"]["priority"] == ["low", "medium", "high"]


def test_add_action_has_no_list_selector_when_no_lists() -> None:
    board = build_tasks_board([], today=TODAY, lists=[])
    add = board["actions"][0]
    assert "list_id" not in add["fields"]
    assert "list_id" not in add["field_options"]
