"""Abstract calendar provider interface.

All providers expose the same three operations behind a common ABC so the
module's MCP tools never need to know which backend is active.  The
``name`` class attribute is used in Event.provider and status reporting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from epicurus_calendar.models import DateTimeRange, Event


class CalendarProvider(ABC):
    """Contract every calendar backend must satisfy."""

    name: str

    @abstractmethod
    async def list_events(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
    ) -> list[Event]:
        """Return all events that overlap *time_range* for *tenant_id*."""

    @abstractmethod
    async def get_event(self, *, tenant_id: str, event_id: str) -> Event | None:
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
    ) -> Event:
        """Persist a new event and return the created domain object."""

    @abstractmethod
    async def find_free_slots(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
    ) -> list[DateTimeRange]:
        """Return time slots of at least *duration_minutes* with no events."""

    @abstractmethod
    async def is_available(self, *, tenant_id: str) -> bool:
        """True when the provider is configured and reachable for *tenant_id*."""
