"""Recurrence math for emulated repeating tasks (ADR-0082).

Provider-neutral helpers over an RFC 5545 RRULE string, mirroring the calendar module's
``recurrence.py`` but for tasks. Tasks recur by *due date* (date-only), and the module
materializes the next instance itself because neither provider expands the rule for it —
Google Tasks has no recurrence field at all, and the local store is a plain table. The
router calls :func:`next_due` when a recurring task is completed to schedule its successor.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from dateutil.rrule import rrulestr

from epicurus_core import get_logger

log = get_logger("epicurus_tasks.recurrence")

# A datetime ``UNTIL`` with a trailing ``Z`` is UTC-aware. Tasks recur by *date* (no wall clock
# or zone), and dateutil requires ``UNTIL`` and the ``dtstart`` anchor to share tz-awareness — so
# a calendar-style ``UNTIL=…Z`` (what the calendar module and this module's own examples write)
# would clash with the naive date anchor. Strip the ``Z`` so the whole rule parses naively; the
# date arithmetic is unaffected (ADR-0082).
_UNTIL_Z = re.compile(r"(UNTIL=\d{8}T\d{6})Z", re.IGNORECASE)


def _date_only_rule(rule: str) -> str:
    """Normalize an RRULE for naive, date-only parsing (drop a UTC ``Z`` on a datetime UNTIL)."""
    return _UNTIL_Z.sub(r"\1", rule)


def validate_rrule(rule: str) -> None:
    """Reject an unparseable RRULE at the tool boundary rather than storing garbage.

    The module expands this rule with ``dateutil`` every time a recurring task is completed,
    so a write-time check gives the agent/operator an immediate, actionable error instead of
    a silently broken series that fails only on the next completion. The rule's *grammar*
    doesn't depend on its start, so an arbitrary ``dtstart`` anchors the parse.
    """
    try:
        rrulestr(f"RRULE:{_date_only_rule(rule)}", dtstart=datetime(2000, 1, 1))
    except Exception as exc:
        raise ValueError(f"invalid recurrence rule {rule!r}: {exc}") from exc


def _as_date(value: str) -> date:
    """Parse a task due value (ISO date or RFC 3339) to a date; the time part is irrelevant."""
    return date.fromisoformat(value[:10])


def next_due(current_due: str | None, rule: str, *, today: str) -> str | None:
    """The next instance's due date (ISO ``YYYY-MM-DD``) after completing an instance.

    Called by the router when a task carrying *rule* is completed, to decide the due date of
    the successor instance it materializes. Returns ``None`` when no successor should be
    created — the series is exhausted (a ``COUNT``/``UNTIL`` rule has no later occurrence), or
    the rule can't be anchored because the completed instance had no due date.

    Args:
        current_due: The completed instance's due date (ISO date / RFC 3339), or ``None``.
        rule: A bare RFC 5545 RRULE string (no ``"RRULE:"`` prefix), e.g. ``"FREQ=WEEKLY"``.
        today: Today's date as an ISO string (injected so this stays clock-free and testable),
            available to the anchoring policy for deciding how to treat a task completed late.

    Returns the next due date as an ISO date string, or ``None`` if there is no next instance.
    """
    # A recurring task with no due date can't be advanced — there's no anchor for the rule.
    # (The tool layer rejects `repeat` without a `due`, so this is a defensive guard.)
    if not current_due:
        return None

    anchor = _as_date(current_due)
    rule_set = rrulestr(
        f"RRULE:{_date_only_rule(rule)}", dtstart=datetime(anchor.year, anchor.month, anchor.day)
    )

    # Anchoring policy (ADR-0082): **skip-missed**. Advance from the later of the completed
    # instance's due date and *today*, so a task completed late rolls forward to the next
    # future occurrence instead of surfacing an already-overdue successor. Completed on time,
    # ``today == anchor`` and this is identical to a plain rule-anchored advance; the two only
    # diverge when the task is overdue (e.g. a daily task completed three days late → tomorrow,
    # not yesterday). To make the cadence fixed regardless of lateness, anchor from ``anchor``
    # alone instead of ``floor``.
    floor = max(anchor, _as_date(today))
    occurrence = rule_set.after(datetime(floor.year, floor.month, floor.day), inc=False)
    return occurrence.date().isoformat() if occurrence else None
