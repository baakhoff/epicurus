"""Abstract calendar provider interface.

All providers expose the same operations behind a common ABC so the module's
MCP tools and the :class:`~epicurus_calendar.providers.router.CollectionRouter`
never need to know which backend is active.  The ``name`` class attribute is
used in ``Event.provider`` and status reporting.

Every read/write takes an optional ``calendar_id`` — the collection within the
account to act on (ADR-0030). ``None`` means the provider's own default (the
local store ignores it; Google falls back to its configured calendar). The
router passes a concrete collection id resolved from the operator's selection.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal

from epicurus_calendar.models import Attendee, DateTimeRange, Event
from epicurus_core import Collection

#: Which occurrences an edit/delete on a recurring event applies to: ``"this"`` (#432) — the
#: single instance the caller identified (the default; least blast radius); ``"following"``
#: (#445) — this occurrence and every later one, splitting the series in two (see
#: ``epicurus_calendar.recurrence``); ``"all"`` (#432) — the whole series (identified by its
#: own id, or resolved from an instance's series). Ignored for a one-off event.
EditScope = Literal["this", "following", "all"]


class CalendarProvider(ABC):
    """Contract every calendar backend must satisfy."""

    name: str

    @abstractmethod
    async def list_events(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        calendar_id: str | None = None,
    ) -> list[Event]:
        """Return all events that overlap *time_range* for *tenant_id*."""

    @abstractmethod
    async def get_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> Event | None:
        """Return the single event with *event_id* for *tenant_id*, or ``None``.

        Backs the entity-ref hover-card resolver and the chat-attachment resolve
        (ADR-0019): both need to fetch one referenced event by its id.
        """

    @abstractmethod
    async def create_event(
        self,
        *,
        tenant_id: str,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
        recurrence: str | None = None,
        attendees: list[Attendee] | None = None,
        recurrence_timezone: str | None = None,
        add_meet: bool = False,
    ) -> Event:
        """Persist a new event and return the created domain object.

        When *all_day* is true, *start*/*end* are UTC-midnight day boundaries with *end*
        exclusive (see :attr:`~epicurus_calendar.models.Event.all_day`); a provider stores
        them as a date-only event (Google's ``date`` fields) rather than timed instants.

        *recurrence* (#432) is an RFC 5545 RRULE string (no ``"RRULE:"`` prefix) making this
        the series' master; ``None`` for a one-off event. *attendees* invites guests
        (``needsAction`` initially); ``None``/empty means no guests. *recurrence_timezone*
        (#446) is the IANA zone *recurrence* anchors its wall-clock expansion in (the
        operator's configured timezone at creation) — meaningful only alongside
        *recurrence*; a provider that expands recurrence itself (Google) ignores it, since
        it always returns correct per-occurrence instants without help. *add_meet* (#444)
        requests a Google Meet conference link (:attr:`~epicurus_calendar.models.Event.meet_url`)
        be attached; a provider with no conferencing backend (local) silently ignores it.
        """

    @abstractmethod
    async def update_event(
        self,
        *,
        tenant_id: str,
        event_id: str,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool | None = None,
        recurrence: str | None = None,
        attendees: list[Attendee] | None = None,
        recurrence_timezone: str | None = None,
        edit_scope: EditScope = "this",
    ) -> Event | None:
        """Apply the given fields to an existing event and return it.

        Only non-``None`` fields are changed; the rest are left as they are. Passing
        *all_day* switches the event between timed and date-only (the caller supplies
        *start*/*end* to match). Returns ``None`` when the event does not exist in this
        provider/collection — the router uses that to try the next source (write where the
        event lives, #208).

        *edit_scope* matters only when *event_id* names an occurrence of a recurring series:
        ``"this"`` (#432) edits just that occurrence (creating an exception if it wasn't one
        already); ``"following"`` (#445) splits the series in two at that occurrence — the
        original series ends just before it, and it plus every later occurrence move to a new
        series carrying the edit; ``"all"`` (#432) edits the whole series in place.
        *recurrence* changes the series' rule and is only meaningful with
        ``edit_scope="all"`` or ``"following"`` (omitted, it continues the original pattern
        for the new tail series); an empty string ``""`` **clears** the rule (#532) — with
        ``"all"`` the master's rule is dropped so the series collapses to a one-off event, with
        ``"following"`` the series ends at this occurrence (earlier occurrences keep the rule,
        this one becomes a standalone event). ``None`` leaves the rule unchanged; the three
        states (unchanged / clear / set) are why the parameter is a sentinel string rather than
        a plain optional. Clearing with ``"this"`` is rejected upstream (a lone occurrence has
        no series rule of its own). *recurrence_timezone* (#446) re-anchors its wall-clock
        expansion zone alongside a set rule (see :meth:`create_event`).
        """

    @abstractmethod
    async def delete_event(
        self,
        *,
        tenant_id: str,
        event_id: str,
        calendar_id: str | None = None,
        edit_scope: EditScope = "this",
    ) -> bool:
        """Delete an event. Returns ``True`` if it existed and was removed, else ``False``.

        ``False`` lets the router fall through to the next enabled source rather than
        report a spurious success (#208). *edit_scope*: ``"this"`` (#432) removes just the
        named occurrence of a recurring series (as an excluded exception); ``"following"``
        (#445) truncates the series so it ends just before that occurrence, removing it and
        every later one; ``"all"`` (#432) removes the whole series.
        """

    @abstractmethod
    async def find_free_slots(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> list[DateTimeRange]:
        """Return time slots of at least *duration_minutes* with no events."""

    @abstractmethod
    async def is_available(self, *, tenant_id: str) -> bool:
        """True when the provider is configured and reachable for *tenant_id*."""

    async def get_timezone(self, *, tenant_id: str) -> str | None:
        """The provider's IANA timezone for *tenant_id*, or ``None`` (ADR-0039).

        Default ``None`` (the local store has no inherent timezone). Google overrides this
        to report the user's Google Calendar timezone, which the core's ``now`` tool uses to
        flag a mismatch with the configured timezone. Best-effort — never raises.
        """
        return None

    @abstractmethod
    async def list_collections(self, *, tenant_id: str) -> list[Collection]:
        """The collections (calendars) this provider exposes for *tenant_id* (ADR-0030).

        Drives the connected-accounts picker. An account-less provider (the local
        store) returns an empty list — it is the silent default, never a selectable
        account.
        """
