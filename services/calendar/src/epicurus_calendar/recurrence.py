"""Provider-neutral helpers for splitting a recurring series in two (#445).

``edit_scope="following"`` (edit/delete this occurrence and every later one) requires
truncating the original series so it ends just before the split point, and — for an edit —
continuing the pattern from there as a new series. Both providers need identical RRULE-string
arithmetic to do this: the local provider expands the result itself in Python, while Google
only needs the same two rule strings to send over the wire (it expands recurrence server-side
either way). Kept pure and provider-neutral so both call the same logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dateutil.rrule import rrule

# RFC 5545 requires a timed UNTIL to be expressed in UTC.
_UNTIL_FORMAT = "%Y%m%dT%H%M%SZ"


def _parts(rule_str: str) -> dict[str, str]:
    """An RRULE string's ``KEY=VALUE`` parts as a dict, preserving order."""
    parts: dict[str, str] = {}
    for part in rule_str.split(";"):
        key, _, value = part.partition("=")
        parts[key] = value
    return parts


def _join(parts: dict[str, str]) -> str:
    return ";".join(f"{key}={value}" for key, value in parts.items())


def truncate_before(rule_str: str, rule: rrule, split_at: datetime) -> str | None:
    """The original series' rule, ending just before *split_at* (#445).

    Sets ``UNTIL`` to the last occurrence strictly before *split_at* and drops any ``COUNT``
    — RFC 5545 forbids the two together, and an explicit ``UNTIL`` alone fully captures the
    new stopping point regardless of what the old ``COUNT`` said. Returns ``None`` when
    *split_at* is *rule*'s very first occurrence: there is no earlier occurrence to keep
    separate, so the caller should treat splitting here as editing/deleting the whole series
    instead of truncating it.
    """
    last_before = rule.before(split_at, inc=False)
    if last_before is None:
        return None
    parts = _parts(rule_str)
    parts.pop("COUNT", None)
    parts["UNTIL"] = last_before.astimezone(UTC).strftime(_UNTIL_FORMAT)
    return _join(parts)


def continue_from(rule_str: str, rule: rrule, split_at: datetime) -> str:
    """The new tail series' rule, continuing from *split_at* to the original's own endpoint.

    An ``UNTIL``-bound or unbounded rule is returned unchanged — its absolute endpoint (or
    lack of one) doesn't depend on where the new series' own ``DTSTART`` begins. A
    ``COUNT``-bound rule is renumbered to just the occurrences from *split_at* onward, since
    the original ``COUNT`` was relative to the *original* ``DTSTART``.
    """
    parts = _parts(rule_str)
    if "COUNT" in parts:
        total = int(parts["COUNT"])
        before = sum(1 for occurrence in rule if occurrence < split_at)
        parts["COUNT"] = str(total - before)
    return _join(parts)
