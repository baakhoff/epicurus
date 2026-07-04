"""Tests for the pure recurrence math (ADR-0082): validate_rrule + next_due.

next_due uses the **skip-missed** anchoring policy — advance from the later of the completed
instance's due date and today, so a late completion rolls forward to a future occurrence rather
than surfacing an already-overdue successor. On-time completion (today == due) is identical to a
plain rule-anchored advance; the two only diverge when the task is overdue.
"""

from __future__ import annotations

import pytest

from epicurus_tasks.recurrence import next_due, validate_rrule

# 2026-07-06 is a Monday; -09 Thursday; -10 Friday; -13 the next Monday.
MON = "2026-07-06"
THU = "2026-07-09"
FRI = "2026-07-10"
NEXT_MON = "2026-07-13"


# ── validate_rrule ────────────────────────────────────────────────────────────


def test_validate_accepts_a_valid_rule() -> None:
    validate_rrule("FREQ=WEEKLY;COUNT=10")  # does not raise


def test_validate_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="invalid recurrence rule"):
        validate_rrule("this is not an rrule")


def test_validate_accepts_calendar_style_until_z_form() -> None:
    # A UTC UNTIL (trailing Z) — what the calendar module and this tool's own docs write — is
    # accepted despite the naive date anchor (ADR-0082 normalizes the Z away).
    validate_rrule("FREQ=DAILY;UNTIL=20261231T000000Z")


def test_until_z_form_still_yields_next_while_live() -> None:
    assert next_due(MON, "FREQ=WEEKLY;UNTIL=20261231T000000Z", today=MON) == NEXT_MON


# ── next_due: on-time completion (today == due) ───────────────────────────────


def test_weekly_on_time_advances_one_week() -> None:
    assert next_due(MON, "FREQ=WEEKLY", today=MON) == NEXT_MON


def test_daily_on_time_advances_one_day() -> None:
    assert next_due(MON, "FREQ=DAILY", today=MON) == "2026-07-07"


def test_monthly_on_time_advances_one_month() -> None:
    assert next_due("2026-01-15", "FREQ=MONTHLY", today="2026-01-15") == "2026-02-15"


def test_monthly_jan31_skips_short_months_rather_than_clamping() -> None:
    # dateutil's FREQ=MONTHLY does not clamp to the last day of a short month — a month with
    # no 31st (Feb, and any 30-day month) is simply skipped, not rolled to its own end (#515).
    # 2026 is not a leap year, so Feb has 28 days: Jan 31 -> Mar 31, Feb entirely skipped.
    assert next_due("2026-01-31", "FREQ=MONTHLY", today="2026-01-31") == "2026-03-31"


def test_weekdays_rule_skips_the_weekend() -> None:
    # Completed Friday → next weekday is the following Monday, not Saturday.
    assert next_due(FRI, "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", today=FRI) == NEXT_MON


# ── next_due: late completion (skip-missed policy) ────────────────────────────


def test_daily_completed_late_skips_to_the_next_future_day() -> None:
    # Due Monday, completed Thursday: the next instance is Friday (not Tuesday) — the missed
    # Tue/Wed/Thu occurrences are skipped rather than surfacing an overdue successor.
    assert next_due(MON, "FREQ=DAILY", today=THU) == FRI


def test_weekly_completed_late_still_lands_on_the_cadence() -> None:
    # A weekly-Monday task completed Thursday still advances to the next Monday.
    assert next_due(MON, "FREQ=WEEKLY", today=THU) == NEXT_MON


# ── next_due: series end + no anchor ──────────────────────────────────────────


def test_count_exhausted_returns_none() -> None:
    # COUNT=1 means a single occurrence (the current one); there is no next instance.
    assert next_due(MON, "FREQ=DAILY;COUNT=1", today=MON) is None


def test_until_in_the_past_returns_none() -> None:
    rule = "FREQ=WEEKLY;UNTIL=20260701T000000Z"
    assert next_due("2026-06-29", rule, today="2026-07-06") is None


def test_no_due_date_cannot_be_anchored() -> None:
    assert next_due(None, "FREQ=DAILY", today=MON) is None
    assert next_due("", "FREQ=WEEKLY", today=MON) is None


def test_rfc3339_due_value_is_accepted() -> None:
    # Google returns due dates as RFC 3339 timestamps; only the date part matters.
    assert next_due("2026-07-06T00:00:00.000Z", "FREQ=WEEKLY", today=MON) == NEXT_MON
