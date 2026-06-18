"""Collection router — fans calendar reads/writes across the operator's selection.

The router holds the always-present local store plus each external provider (Google)
and routes per the operator's stored selection (ADR-0030), fetched from the core via a
:class:`CollectionPrefsSource` (the module's ``PlatformClient``):

* **reads** (``list_events``) overlay every *enabled* collection — calendar is a
  ``multi`` module — falling back to the local store when nothing is enabled;
* **writes** (``create_event``) and ``find_free_slots`` target the single *active*
  collection, falling back to local when none is set;
* ``get_event`` searches the active, then the other enabled, then local — so a
  referenced event resolves wherever it lives.

It satisfies :class:`CalendarProvider`, so the module's tools and page treat it like
any other backend; the per-call ``calendar_id`` argument is resolved internally from
the selection, so a value passed in is ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from epicurus_calendar.models import DateTimeRange, Event
from epicurus_calendar.providers.base import CalendarProvider
from epicurus_core import LOCAL_ACCOUNT, Collection, CollectionPrefs, CollectionRef, get_logger

log = get_logger("epicurus_calendar.router")

_LOCAL_REF = CollectionRef(account=LOCAL_ACCOUNT)


class CollectionPrefsSource(Protocol):
    """Returns the operator's stored collection selection (the module's PlatformClient)."""

    async def get_collections(self) -> CollectionPrefs: ...


class CollectionRouter(CalendarProvider):
    """Routes calendar ops across local + external providers per the operator's selection."""

    name = "calendar"

    def __init__(
        self,
        *,
        local: CalendarProvider,
        external: dict[str, CalendarProvider],
        prefs: CollectionPrefsSource,
    ) -> None:
        self._local = local
        self._external = external
        self._prefs = prefs

    def _provider_for(self, account: str) -> CalendarProvider | None:
        """The provider backing *account*, or ``None`` if it isn't configured/connected."""
        if account == LOCAL_ACCOUNT:
            return self._local
        return self._external.get(account)

    async def list_events(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        calendar_id: str | None = None,
    ) -> list[Event]:
        prefs = await self._load_prefs()
        targets = prefs.enabled or [_LOCAL_REF]
        events: list[Event] = []
        for ref in targets:
            provider = self._provider_for(ref.account)
            if provider is None:
                continue  # an unknown / disconnected account is skipped, not fatal
            try:
                events.extend(
                    await provider.list_events(
                        tenant_id=tenant_id,
                        time_range=time_range,
                        calendar_id=ref.collection or None,
                    )
                )
            except Exception as exc:
                log.warning(
                    "calendar read failed; skipping this source (#209)",
                    account=ref.account,
                    collection=ref.collection,
                    error=str(exc),
                )
        events.sort(key=lambda e: e.start)
        return events

    async def get_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> Event | None:
        prefs = await self._load_prefs()
        # Search the active collection first, then the rest of the enabled set, then the
        # local store — a referenced event resolves wherever it lives.
        refs: list[CollectionRef] = []
        if prefs.active is not None:
            refs.append(prefs.active)
        refs.extend(prefs.enabled)
        refs.append(_LOCAL_REF)
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (ref.account, ref.collection)
            if key in seen:
                continue
            seen.add(key)
            provider = self._provider_for(ref.account)
            if provider is None:
                continue
            try:
                event = await provider.get_event(
                    tenant_id=tenant_id, event_id=event_id, calendar_id=ref.collection or None
                )
            except Exception as exc:
                log.warning(
                    "calendar lookup failed; trying next source (#209)",
                    account=ref.account,
                    collection=ref.collection,
                    error=str(exc),
                )
                continue
            if event is not None:
                return event
        return None

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
        ref = await self._active_ref()
        provider = self._provider_for(ref.account) or self._local
        return await provider.create_event(
            tenant_id=tenant_id,
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            calendar_id=ref.collection or None,
        )

    async def find_free_slots(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> list[DateTimeRange]:
        # Free/busy is computed for the active calendar — the one a new event lands on.
        ref = await self._active_ref()
        provider = self._provider_for(ref.account) or self._local
        return await provider.find_free_slots(
            tenant_id=tenant_id,
            time_range=time_range,
            duration_minutes=duration_minutes,
            calendar_id=ref.collection or None,
        )

    async def is_available(self, *, tenant_id: str) -> bool:
        # The local default is always available, so the calendar is never "unavailable".
        return True

    async def list_collections(self, *, tenant_id: str) -> list[Collection]:
        # Discovery is driven from the external providers directly (see /accounts); the
        # router itself is not a selectable account.
        return []

    async def _active_ref(self) -> CollectionRef:
        prefs = await self._load_prefs()
        return prefs.active or _LOCAL_REF

    async def _load_prefs(self) -> CollectionPrefs:
        """The operator's selection, falling back to local-only if the core is unreachable.

        A prefs read must never break a calendar read: if the core is down or errors, the
        module quietly falls back to its silent local default (local-first).
        """
        try:
            return await self._prefs.get_collections()
        except Exception as exc:
            log.warning("collection prefs unavailable; using local default", error=str(exc))
            return CollectionPrefs()
