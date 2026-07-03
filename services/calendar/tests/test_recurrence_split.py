"""Unit tests for ``epicurus_calendar.recurrence`` (#445) — the pure RRULE-splitting
arithmetic shared by both providers' ``edit_scope="following"`` implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dateutil.rrule import rrule, rrulestr

from epicurus_calendar.recurrence import continue_from, truncate_before


def _dt(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 7, day, hour, 0, tzinfo=UTC)


def _rule(rule_str: str, dtstart: datetime) -> rrule:
    parsed = rrulestr(f"RRULE:{rule_str}", dtstart=dtstart)
    assert isinstance(parsed, rrule)
    return parsed


# ── truncate_before ───────────────────────────────────────────────────────────────


def test_truncate_before_sets_until_to_the_prior_occurrence() -> None:
    rule_str = "FREQ=WEEKLY;COUNT=4"  # occurrences: 6, 13, 20, 27
    rule = _rule(rule_str, _dt(6))
    truncated = truncate_before(rule_str, rule, _dt(20))
    assert truncated is not None
    assert "COUNT" not in truncated
    assert "UNTIL=20260713T090000Z" in truncated
    reparsed = _rule(truncated, _dt(6))
    assert [occ.day for occ in reparsed] == [6, 13]


def test_truncate_before_returns_none_at_the_first_occurrence() -> None:
    rule_str = "FREQ=WEEKLY;COUNT=4"
    rule = _rule(rule_str, _dt(6))
    assert truncate_before(rule_str, rule, _dt(6)) is None


def test_truncate_before_preserves_an_until_bound_rule() -> None:
    rule_str = "FREQ=DAILY;UNTIL=20260710T090000Z"
    rule = _rule(rule_str, _dt(6))
    truncated = truncate_before(rule_str, rule, _dt(8))
    assert truncated is not None
    assert truncated == "FREQ=DAILY;UNTIL=20260707T090000Z"


def test_truncate_before_bounds_an_unbounded_rule() -> None:
    rule_str = "FREQ=DAILY"
    rule = _rule(rule_str, _dt(6))
    truncated = truncate_before(rule_str, rule, _dt(10))
    assert truncated == "FREQ=DAILY;UNTIL=20260709T090000Z"


# ── continue_from ─────────────────────────────────────────────────────────────────


def test_continue_from_renumbers_count() -> None:
    rule_str = "FREQ=WEEKLY;COUNT=4"  # 6, 13, 20, 27 — split at the 3rd (20) leaves 2.
    rule = _rule(rule_str, _dt(6))
    assert continue_from(rule_str, rule, _dt(20)) == "FREQ=WEEKLY;COUNT=2"


def test_continue_from_preserves_other_parts_alongside_count() -> None:
    rule_str = "FREQ=WEEKLY;INTERVAL=2;COUNT=5"
    rule = _rule(rule_str, _dt(6))
    third = list(rule)[2]  # 5 fortnightly occurrences from July 6: 6, 20, Aug 3, 17, 31
    tail = continue_from(rule_str, rule, third)
    assert tail == "FREQ=WEEKLY;INTERVAL=2;COUNT=3"  # 5 total, 2 before the 3rd


def test_continue_from_leaves_an_until_bound_rule_unchanged() -> None:
    rule_str = "FREQ=DAILY;UNTIL=20260710T090000Z"
    rule = _rule(rule_str, _dt(6))
    assert continue_from(rule_str, rule, _dt(8)) == rule_str


def test_continue_from_leaves_an_unbounded_rule_unchanged() -> None:
    rule_str = "FREQ=DAILY"
    rule = _rule(rule_str, _dt(6))
    assert continue_from(rule_str, rule, _dt(10)) == rule_str
