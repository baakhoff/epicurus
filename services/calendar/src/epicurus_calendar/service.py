"""Calendar module — MCP tool surface, entity-ref resolver, and attachment source.

Registers three provider-agnostic tools the agent can call:

* ``calendar_list_events``  — list events in a rolling window.
* ``calendar_create_event`` — create a new event from natural-language inputs.
* ``calendar_find_free``    — find open time slots of a requested duration.

The tools delegate entirely to the active ``CalendarProvider``; swapping the
provider (local ↔ Google ↔ future CalDAV) requires no tool changes.

Since **v0.4** the module also speaks the entity-reference contract (ADR-0019):
``calendar_list_events`` returns events as entity-reference chips, the module
resolves a referenced event to a core **hover-card**, and it is a
**chat-attachment source** — the helpers that back those surfaces live here
(``event_hover_card``, ``event_attachment``, ``calendar_attachments``,
``fetch_event``) so they are unit-testable without a running app.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import Field

from epicurus_calendar.models import DateTimeRange, Event
from epicurus_calendar.providers.base import CalendarProvider
from epicurus_core import (
    Account,
    AccountsView,
    CollectionsSpec,
    EntityRef,
    EpicurusModule,
    HoverCard,
    HoverCardDetail,
    PageSpec,
    UiSection,
    tool_envelope,
)

MODULE_NAME = "calendar"
CALENDAR_PAGE_ID = "calendar"

# Tool-parameter aliases that surface JSON-Schema hints to the core-rendered form (#208):
# the web SchemaForm renders an ISO-8601 string with ``format: date-time`` as a native
# datetime picker, and a ``multiline`` string as a textarea.
_IsoDateTime = Annotated[str, Field(json_schema_extra={"format": "date-time"})]
_Multiline = Annotated[str, Field(json_schema_extra={"format": "multiline"})]

# The external providers the calendar module can connect (ADR-0030); ``local`` is the
# implicit default and is never listed. Maps the account id to its shell display label.
PROVIDER_LABELS = {"google": "Google"}

# The kind every calendar entity-reference and attachment carries (ADR-0019).
EVENT_KIND = "event"

# A single page fetch must not scan an unbounded range. The shell's month grid spans
# ~42 days and never needs more than a few weeks; this cap keeps a stray or hostile
# request bounded without clipping any real view.
_MAX_RANGE_DAYS = 92

# Chat-attachment picker bounds (ADR-0019): the composer lists *upcoming* events to
# attach, so the window looks forward only and is capped to keep the menu manageable.
_ATTACH_RANGE_DAYS = 30
_ATTACH_LIMIT = 50


def build_module(provider: CalendarProvider, tenant_id: str) -> EpicurusModule:
    """Build the calendar module and register its MCP tools.

    Args:
        provider: The calendar backend the tools read/write through — in the running
            service a :class:`~epicurus_calendar.providers.router.CollectionRouter`
            that fans across the operator's enabled/active collections (ADR-0030); a
            single provider in unit tests.
        tenant_id: Default tenant for all tool calls.
    """
    module = EpicurusModule(
        MODULE_NAME,
        version="0.7.1",
        description=(
            "Provider-neutral calendar: list events, create events, and find"
            " free time slots. Backed by a local store (no account needed) plus"
            " any Google calendars the operator connects and enables."
        ),
        ui=UiSection(
            icon="calendar",
            summary=(
                "Calendar with a built-in local store (no account needed) plus any"
                " **Google** calendars you connect. Choose which calendars to show and"
                " which one new events land on below; the agent can list events, create"
                " them, and find a free slot — all from natural language."
            ),
            # No config_schema: there is no provider dropdown any more (ADR-0030). The
            # operator connects accounts and toggles calendars in the connected-accounts
            # section the shell renders from `collections`.
            status_url="/status",
            # No manifest actions: the one read tool (`calendar_list_events`) now returns
            # an entity-reference envelope (chips), which the module card's plain-text
            # result panel can't render — events are surfaced through chat instead. This
            # mirrors mail keeping `mail_search` out of its actions (ADR-0019).
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
        # Resolve a referenced event to a hover-card at GET /resolve/event/{id} (ADR-0019).
        resolver=True,
        # Be a chat-attachment source: GET /attachments (picker) + /attachments/{id} (ADR-0019).
        attachable=True,
        # Account/collection model (ADR-0030): a silent local default plus connectable
        # Google calendars the operator toggles/switches. Calendar overlays every enabled
        # calendar on read (multi) and writes to the active one. Serves GET /accounts.
        collections=CollectionsSpec(noun="calendar", multi=True, providers=["google"]),
        # The Google API scope the shell requests when connecting an account (#241); the
        # core adds the default identity scopes. Without this, connecting grants only an
        # identity token and the Calendar API returns 403.
        oauth_scopes={"google": ["https://www.googleapis.com/auth/calendar"]},
    )

    @module.tool()
    async def calendar_list_events(range_days: int = 7) -> str:
        """List calendar events in the next *range_days* days (default 7).

        Returns the events as entity-reference chips (ADR-0019): hover a chip for
        the event's hover-card, click it to open the event in the side panel. Each
        chip carries the event id, so you can refer to an event later without
        listing again. The accompanying text lists each event's title and time.

        Args:
            range_days: How many days ahead to look (1-90).

        Returns a tool envelope whose chips reference the matching events.
        """
        capped = min(max(range_days, 1), 90)
        now = datetime.now(tz=UTC)
        time_range = DateTimeRange(start=now, end=now + timedelta(days=capped))
        events = await provider.list_events(tenant_id=tenant_id, time_range=time_range)
        if not events:
            return tool_envelope(f"No events in the next {capped} day(s).", [])
        refs = [event_entity_ref(e) for e in events]
        lines = [
            f"- {e.title} ({_format_when(e)})" + (f" @ {e.location}" if e.location else "")
            for e in events
        ]
        text = f"Found {len(events)} event(s):\n" + "\n".join(lines)
        return tool_envelope(text, refs)

    @module.tool()
    async def calendar_create_event(
        title: str,
        start: _IsoDateTime,
        end: _IsoDateTime,
        location: str | None = None,
        description: _Multiline | None = None,
    ) -> dict[str, Any]:
        """Create a calendar event and return the created event.

        The event lands on the active calendar (the local store by default, or the
        Google calendar the operator has set active — ADR-0030).

        Args:
            title: Event title (required).
            start: Start time in ISO-8601 format, e.g. ``"2025-06-15T10:00:00+00:00"``.
            end: End time in ISO-8601 format, e.g. ``"2025-06-15T11:00:00+00:00"``.
            location: Optional location string (address, room name, or URL).
            description: Optional description or agenda.

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
    async def calendar_update_event(
        event_id: str,
        title: str | None = None,
        start: _IsoDateTime | None = None,
        end: _IsoDateTime | None = None,
        location: str | None = None,
        description: _Multiline | None = None,
    ) -> dict[str, Any]:
        """Edit an existing event and return the updated event.

        Only the fields you pass are changed; the rest are left as they are. The event
        is found and edited wherever it lives across the enabled calendars (#208).

        Args:
            event_id: The id of the event to edit (from a listing or the page).
            title: New title, if changing it.
            start: New start time (ISO-8601), if changing it.
            end: New end time (ISO-8601), if changing it.
            location: New location, if changing it.
            description: New description, if changing it.

        Returns the updated event dict. Raises if no such event exists.
        """
        event = await provider.update_event(
            tenant_id=tenant_id,
            event_id=event_id,
            title=title,
            start=datetime.fromisoformat(start) if start else None,
            end=datetime.fromisoformat(end) if end else None,
            description=description,
            location=location,
        )
        if event is None:
            raise ValueError(f"event {event_id!r} not found")
        return event.model_dump(mode="json")

    @module.tool()
    async def calendar_delete_event(event_id: str) -> dict[str, Any]:
        """Delete a calendar event by its id.

        Removes the event wherever it lives across the enabled calendars (#208).

        Args:
            event_id: The id of the event to delete.

        Returns ``{"deleted": true, "id": ...}`` on success; raises if no such event
        exists.
        """
        deleted = await provider.delete_event(tenant_id=tenant_id, event_id=event_id)
        if not deleted:
            raise ValueError(f"event {event_id!r} not found")
        return {"deleted": True, "id": event_id}

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


# ── Entity references, hover-cards & attachments (ADR-0019) ───────────────────


class EventNotFound(Exception):
    """Raised when an event id does not resolve for the active provider/tenant."""


def _format_when(event: Event) -> str:
    """A compact, human ``when`` line for an event (chips, hover-cards, excerpts)."""
    start, end = event.start, event.end
    if start.date() == end.date():
        return f"{start:%a %d %b %Y, %H:%M}-{end:%H:%M}"
    return f"{start:%a %d %b %Y %H:%M} → {end:%a %d %b %Y %H:%M}"


def event_entity_ref(event: Event) -> EntityRef:
    """The chip an agent turn carries for a listed event (ADR-0019)."""
    summary = _format_when(event)
    if event.location:
        summary = f"{summary} · {event.location}"
    return EntityRef(
        ref_id=event.id,
        module=MODULE_NAME,
        kind=EVENT_KIND,
        title=event.title,
        summary=summary,
    )


def event_hover_card(event: Event) -> dict[str, Any]:
    """The core hover-card / entity-detail envelope for an event (ADR-0019).

    Core-owned, uniform shape: the module supplies the data, the shell renders the
    inline hover-card and the panel's entity-detail view from it.
    """
    details = [HoverCardDetail(label="When", value=_format_when(event))]
    if event.location:
        details.append(HoverCardDetail(label="Location", value=event.location))
    details.append(HoverCardDetail(label="Calendar", value=event.provider))
    return HoverCard(
        title=event.title,
        description=event.description or "",
        details=details,
    ).model_dump()


def event_excerpt(event: Event) -> str:
    """A short plain-text rendering of an event for the agent's turn context."""
    lines = [event.title, _format_when(event)]
    if event.location:
        lines.append(f"Location: {event.location}")
    if event.description:
        lines.extend(["", event.description])
    return "\n".join(lines)


def event_attachment_item(event: Event) -> dict[str, str]:
    """One picker row the composer lists for the attachment source (ADR-0019)."""
    return {"ref_id": event.id, "kind": EVENT_KIND, "title": event.title}


def event_attachment(event: Event) -> dict[str, str]:
    """The resolve payload the agent injects when an attached event is expanded."""
    return {"title": event.title, "excerpt": event_excerpt(event)}


async def fetch_event(provider: CalendarProvider, *, tenant_id: str, ref_id: str) -> Event:
    """Fetch one event by id, raising :class:`EventNotFound` when it does not exist."""
    event = await provider.get_event(tenant_id=tenant_id, event_id=ref_id)
    if event is None:
        raise EventNotFound(ref_id)
    return event


async def calendar_attachments(
    provider: CalendarProvider,
    *,
    tenant_id: str,
    now: datetime | None = None,
    range_days: int = _ATTACH_RANGE_DAYS,
    limit: int = _ATTACH_LIMIT,
) -> list[dict[str, str]]:
    """Picker for the chat-attachment composer (ADR-0019): upcoming events as items.

    Returns up to *limit* events overlapping the next *range_days* days as
    ``{ref_id, kind, title}`` rows. The agent later resolves the chosen one through
    ``GET /attachments/{ref_id}`` into the turn's context.

    Args:
        provider: The active calendar backend.
        tenant_id: Tenant whose events to offer.
        now: Reference instant for the forward window (injected in tests).
        range_days: How many days ahead to offer.
        limit: Maximum number of items returned.
    """
    ref = now or datetime.now(tz=UTC)
    time_range = DateTimeRange(start=ref, end=ref + timedelta(days=range_days))
    events = await provider.list_events(tenant_id=tenant_id, time_range=time_range)
    return [event_attachment_item(e) for e in events[:limit]]


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
    ref = now or datetime.now(tz=UTC)
    time_range = _resolve_range(start, end, now=ref)
    events = await provider.list_events(tenant_id=tenant_id, time_range=time_range)
    # With the account/collection model a page can overlay several calendars, so the
    # "provider" label reflects the sources actually present (ADR-0030) rather than a
    # single backend name; it defaults to the local store when the window is empty.
    sources = sorted({e.provider for e in events})
    return {
        "title": "Calendar",
        "provider": ", ".join(sources) if sources else "local",
        "range": {
            "start": time_range.start.isoformat(),
            "end": time_range.end.isoformat(),
        },
        # Per-event Edit/Delete actions (#208) — the core renders them in the event
        # detail; the shell invokes the named MCP tool through the core's tool proxy.
        "events": [_event_with_actions(e) for e in events],
        # Page-level "New event" action; defaults the time to the next round hour.
        "actions": [_new_event_action(ref)],
    }


_EVENT_FIELDS = ["title", "start", "end", "location", "description"]


def _event_with_actions(event: Event) -> dict[str, Any]:
    """An event dict plus its Edit/Delete actions for the editable calendar (#208)."""
    data = event.model_dump(mode="json")
    data["actions"] = [
        {
            "tool": "calendar_update_event",
            "label": "Edit",
            "icon": "pencil",
            "form": True,
            "args": {"event_id": event.id},
            "fields": _EVENT_FIELDS,
            "form_values": {
                "title": event.title,
                "start": event.start.isoformat(),
                "end": event.end.isoformat(),
                "location": event.location or "",
                "description": event.description or "",
            },
        },
        {
            "tool": "calendar_delete_event",
            "label": "Delete",
            "icon": "trash",
            "intent": "danger",
            "confirm": f"Delete {event.title!r}? This can't be undone.",
            "args": {"event_id": event.id},
        },
    ]
    return data


def _new_event_action(now: datetime) -> dict[str, Any]:
    """The page-level "New event" action, prefilled with a sensible default time."""
    start = (now.astimezone(UTC) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    return {
        "tool": "calendar_create_event",
        "label": "New event",
        "icon": "plus",
        "intent": "primary",
        "form": True,
        "fields": _EVENT_FIELDS,
        "form_values": {"start": start.isoformat(), "end": end.isoformat()},
    }


async def calendar_accounts(
    external: Mapping[str, CalendarProvider], *, tenant_id: str
) -> AccountsView:
    """The connected-accounts view backing ``GET /accounts`` (ADR-0030).

    One :class:`Account` per supported external provider, ``connected`` from the live
    OAuth check and ``collections`` listed only when connected. ``local`` is the silent
    default and is never included.
    """
    accounts: list[Account] = []
    for account_id, provider in external.items():
        connected = await provider.is_available(tenant_id=tenant_id)
        collections = await provider.list_collections(tenant_id=tenant_id) if connected else []
        accounts.append(
            Account(
                account=account_id,
                provider=account_id,
                label=PROVIDER_LABELS.get(account_id, account_id.title()),
                connected=connected,
                collections=collections,
            )
        )
    return AccountsView(noun="calendar", multi=True, accounts=accounts)


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
