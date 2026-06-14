"""Tests for build_tasks_board — the `board` archetype payload (ADR-0018).

Pure and deterministic given ``today``, so the due-date bucketing is exercised
here without a database or a clock.
"""

from __future__ import annotations

from epicurus_tasks.models import Task
from epicurus_tasks.service import TASKS_PAGE_ID, build_tasks_board

TODAY = "2026-06-14"


def _task(task_id: str, title: str, *, due: str | None = None, notes: str | None = None) -> Task:
    return Task(id=task_id, title=title, due=due, notes=notes)


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
    assert complete["args"] == {"task_id": "t1"}
    assert "form" not in complete  # one-tap, no form

    assert edit["form"] is True
    assert edit["args"] == {"task_id": "t1"}
    assert edit["fields"] == ["title", "notes", "due"]
    assert edit["form_values"]["title"] == "Buy milk"
    assert edit["form_values"]["notes"] == "2 litres"


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


def test_board_offers_add_action_even_when_empty() -> None:
    board = build_tasks_board([], today=TODAY)
    assert board["title"] == "Tasks"
    assert board["columns"] == []
    add = board["actions"][0]
    assert add["tool"] == "tasks_add"
    assert add["intent"] == "primary"
    assert add["form"] is True
    assert add["fields"] == ["title", "notes", "due"]
