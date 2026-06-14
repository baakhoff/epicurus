"""Calendar module — MCP tool surface.

Registers three provider-agnostic tools the agent can call:

* ``calendar_list_events``  — list events in a rolling window.
* ``calendar_create_event`` — create a new event from natural-language inputs.
* ``calendar_find_free``    — find open time slots of a requested duration.

The tools delegate entirely to the active ``CalendarProvider``; swapping the
provider (local ↔ Google ↔ future CalDAV) requires no tool changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from epicurus_calendar.models import DateTimeRange
from epicurus_calendar.providers.base import CalendarProvider
from epicurus_core import EpicurusModule, PageSpec, UiAction, UiSection

MODULE_NAME = "calendar"
CALENDAR_PAGE_ID = "calendar"

# A single page fetch must not scan an unbounded range. The shell's month grid spans
# ~42 days and never needs more than a few weeks; this cap keeps a stray or hostile
# request bounded without clipping any real view.
_MAX_RANGE_DAYS = 92


def build_module(provider: CalendarProvider, tenant_id: str) -> EpicurusModule:
    """Build the calendar module and register its MCP tools.

    Args:
        provider: The active calendar backend (local or Google).
        tenant_id: Default tenant for all tool calls.
    """
    module = EpicurusModule(
        MODULE_NAME,
        version="0.2.0",
        description=(
            "Provider-neutral calendar: list events, create events, and find"
            " free time slots. Supports a local store (no account needed) and"
            " Google Calendar."
        ),
        ui=UiSection(
            icon="calendar",
            summary=(
                f"Calendar access via the **{provider.name}** provider."
                " List upcoming events, create new ones, or ask the agent to"
                " find a free slot — all from natural language."
            ),
            config_schema={
                "type": "object",
                "properties": {
                    "calendar_provider": {
                        "type": "string",
                        "title": "Provider",
                        "description": (
                            '"local" for the built-in Postgres store (no account'
                            ' needed) or "google" to use Google Calendar'
                            " (requires OAuth connection)."
                        ),
                        "enum": ["local", "google"],
                        "default": "local",
                    },
                    "calendar_google_id": {
                        "type": "string",
                        "title": "Google Calendar ID",
                        "description": (
                            'Google Calendar to use. "primary" is the default'
                            " calendar. Only relevant when provider is google."
                        ),
                        "default": "primary",
                    },
                },
            },
            status_url="/status",
            actions=[
                UiAction(
                    tool="calendar_list_events",
                    label="List events",
                    description="Show the next 7 days of calendar events.",
                    intent="default",
                ),
            ],
        ),
        # A left-nav Calendar page (ADR-0018): the module supplies events in a range,
        # the core shell renders the month / week / agenda views. No module markup.
        pages=[
            PageSpec(
                id=CALENDAR_PAGE_ID,
                title="Calendar",
                archetype="calendar",
                icon="calendar",
                nav_order=40,
            )
        ],
    )

    @module.tool()
    async def calendar_list_events(range_days: int = 7) -> list[dict[str, Any]]:
        """List calendar events in the next *range_days* days (default 7).

        Returns events sorted by start time, each with ``id``, ``title``,
        ``start``, ``end``, ``description``, ``location``, and ``provider``
        fields.  Times are ISO-8601 strings.

        Args:
            range_days: How many days ahead to look (1-90).

        Returns a list of event dicts, or an empty list when none exist.
        """
        capped = min(max(range_days, 1), 90)
        now = datetime.now(tz=UTC)
        time_range = DateTimeRange(start=now, end=now + timedelta(days=capped))
        events = await provider.list_events(tenant_id=tenant_id, time_range=time_range)
        return [e.model_dump(mode="json") for e in events]

    @module.tool()
    async def calendar_create_event(
        title: str,
        start: str,
        end: str,
        description: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Create a calendar event and return the created event.

        Args:
            title: Event title (required).
            start: Start time in ISO-8601 format, e.g. ``"2025-06-15T10:00:00+00:00"``.
            end: End time in ISO-8601 format, e.g. ``"2025-06-15T11:00:00+00:00"``.
            description: Optional description or agenda.
            location: Optional location string (address, room name, or URL).

        Returns the created event dict with all fields populated.
        """
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        event = await provider.create_event(
            tenant_id=tenant_id,
            title=title,
            start=start_dt,
            end=end_dt,
            description=description,
            location=location,
        )
        return event.model_dump(mode="json")

    @module.tool()
    async def calendar_find_free(
        duration_minutes: int = 60,
        range_days: int = 7,
    ) -> list[dict[str, Any]]:
        """Find free time slots of at least *duration_minutes* in the next *range_days* days.

        Checks the active calendar for busy periods and returns open windows
        large enough to schedule a meeting of the requested length.

        Args:
            duration_minutes: Minimum slot length in minutes (default 60).
            range_days: How many days ahead to search (1-90, default 7).

        Returns a list of ``{start, end}`` dicts (ISO-8601 strings) for each
        available window, ordered chronologically.
        """
        capped_range = min(max(range_days, 1), 90)
        capped_dur = min(max(duration_minutes, 1), 1440)
        now = datetime.now(tz=UTC)
        time_range = DateTimeRange(start=now, end=now + timedelta(days=capped_range))
        slots = await provider.find_free_slots(
            tenant_id=tenant_id,
            time_range=time_range,
            duration_minutes=capped_dur,
        )
        return [s.model_dump(mode="json") for s in slots]

    return module


async def calendar_page(
    provider: CalendarProvider,
    *,
    tenant_id: str,
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the ``calendar`` archetype's page data (ADR-0018): events in a range.

    The shell drives navigation by requesting a ``[start, end)`` window (ISO-8601);
    when it omits them the page falls back to the current month. The module supplies
    data only — the core renders the month / week / agenda views from this shape::

        {"title", "provider", "range": {"start", "end"}, "events": [Event, ...]}

    Args:
        provider: The active calendar backend.
        tenant_id: Tenant whose events to return.
        start: Range start (ISO-8601); with *end*, the window to list.
        end: Range end (ISO-8601, exclusive).
        now: Reference instant for the default range (injected in tests).

    Raises:
        ValueError: if *start*/*end* are unparseable or ``end <= start``.
    """
    time_range = _resolve_range(start, end, now=now or datetime.now(tz=UTC))
    events = await provider.list_events(tenant_id=tenant_id, time_range=time_range)
    return {
        "title": "Calendar",
        "provider": provider.name,
        "range": {
            "start": time_range.start.isoformat(),
            "end": time_range.end.isoformat(),
        },
        "events": [e.model_dump(mode="json") for e in events],
    }


def _resolve_range(start: str | None, end: str | None, *, now: datetime) -> DateTimeRange:
    """Parse the requested ``[start, end)`` window, or default to *now*'s month."""
    if start is None or end is None:
        return _month_bounds(now)
    start_dt = _parse_instant(start)
    end_dt = _parse_instant(end)
    if end_dt <= start_dt:
        raise ValueError("end must be after start")
    # Clamp an over-wide window rather than reject it: the shell's views never need
    # more than a few weeks, but a stray request shouldn't scan years of events.
    if end_dt - start_dt > timedelta(days=_MAX_RANGE_DAYS):
        end_dt = start_dt + timedelta(days=_MAX_RANGE_DAYS)
    return DateTimeRange(start=start_dt, end=end_dt)


def _parse_instant(value: str) -> datetime:
    """Parse an ISO-8601 timestamp to a timezone-aware datetime (UTC if naive)."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _month_bounds(now: datetime) -> DateTimeRange:
    """The ``[first-of-month, first-of-next-month)`` window around *now*, in UTC."""
    month_start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    return DateTimeRange(start=month_start, end=next_month)
