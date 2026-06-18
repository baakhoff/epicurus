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

from epicurus_calendar.models import DateTimeRange, Event
from epicurus_core import Collection


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
    ) -> Event:
        """Persist a new event and return the created domain object."""

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

    @abstractmethod
    async def list_collections(self, *, tenant_id: str) -> list[Collection]:
        """The collections (calendars) this provider exposes for *tenant_id* (ADR-0030).

        Drives the connected-accounts picker. An account-less provider (the local
        store) returns an empty list — it is the silent default, never a selectable
        account.
        """
