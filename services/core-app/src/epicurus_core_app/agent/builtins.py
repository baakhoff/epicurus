"""Core built-in agent tools (ADR-0039).

Tools the core provides directly, alongside the module tools discovered over MCP. Unlike
module tools they are dispatched in-process (no HTTP). The first is ``now``: the agent has
no inherent notion of the current date/time, so without it the model guesses the date from
its training cutoff and stores times in a guessed timezone.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from epicurus_core import get_logger

log = get_logger("epicurus_core_app.agent.builtins")

# The OpenAI-style function spec the gateway sends to the model.
NOW_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "now",
        "description": (
            "Get the current date and time. Call this whenever a request involves the "
            "current date or a relative time (today, tomorrow, next week, 'at 19:00', "
            "'in 2 hours') so dates and times are correct. Returns the time in the "
            "operator's configured timezone; if a connected calendar uses a different "
            "timezone that is reported too. Pass `timezone` to get the time in a specific "
            "IANA zone instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": (
                        "Optional IANA timezone name (e.g. 'Europe/Belgrade') to report "
                        "instead of the operator's configured timezone."
                    ),
                }
            },
        },
    },
}

#: Returns the operator's configured IANA timezone.
TimezoneProvider = Callable[[], Awaitable[str]]
#: Returns the connected calendar's IANA timezone, or ``None`` (best-effort).
CalendarTzProvider = Callable[[], Awaitable[str | None]]

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_UTC = ZoneInfo("UTC")


def _resolve_zone(name: str) -> tuple[ZoneInfo, str]:
    """Return ``(ZoneInfo, effective_name)``, falling back to UTC on an unknown zone."""
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone; falling back to UTC", timezone=name)
        return _UTC, "UTC"


def make_now_handler(
    tz_provider: TimezoneProvider,
    calendar_tz_provider: CalendarTzProvider,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Build the ``now`` handler closed over its timezone + calendar-tz sources.

    The handler reports the current time in the operator's configured timezone (or an
    explicit ``timezone`` argument). It also reports the connected calendar's timezone and
    a note when it differs from the configured one, so the agent creates events at the
    intended local time. The calendar lookup is best-effort — any failure is omitted, never
    raised.
    """

    async def handler(arguments: dict[str, Any]) -> str:
        configured = await tz_provider()
        requested = arguments.get("timezone")
        wanted = requested if isinstance(requested, str) and requested.strip() else configured
        zone, zone_name = _resolve_zone(wanted)
        now = datetime.now(tz=zone)
        payload: dict[str, Any] = {
            "datetime": now.isoformat(timespec="seconds"),
            "timezone": zone_name,
            "utc": now.astimezone(_UTC).isoformat(timespec="seconds"),
            "weekday": _WEEKDAYS[now.weekday()],
        }
        # Best-effort: surface the calendar's tz when it differs, so the model knows which
        # zone new events land in. A calendar hiccup must never break `now`.
        try:
            calendar_tz = await calendar_tz_provider()
        except Exception as exc:
            log.warning("calendar timezone lookup failed", error=str(exc))
            calendar_tz = None
        if calendar_tz and calendar_tz != zone_name:
            payload["calendar_timezone"] = calendar_tz
            payload["timezone_note"] = (
                f"The connected calendar uses {calendar_tz}, which differs from the "
                f"configured timezone {zone_name}. Create calendar events in {calendar_tz} "
                "unless the user says otherwise."
            )
        return json.dumps(payload)

    return handler
