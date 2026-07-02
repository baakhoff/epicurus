"""Collection router — fans calendar reads/writes across the operator's selection.

The router holds the always-present local store plus each external provider (Google)
and routes per the operator's stored selection (ADR-0030), fetched from the core via a
:class:`CollectionPrefsSource` (the module's ``PlatformClient``):

* **reads** (``list_events``) overlay every *enabled* collection — calendar is a
  ``multi`` module — falling back to the local store when nothing is enabled;
* **writes** (``create_event``) and ``find_free_slots`` target the single *active*
  collection; with none set they prefer a connected external calendar (the first
  enabled one, else a connected provider's default) and fall back to local only
  when nothing external is connected (#433);
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

# Separates account from collection in a calendar-picker token (e.g. ``google:primary``).
# A token names a write target on the create form so the operator can pick which calendar a
# new event lands on, overriding the active default for that one call. Google calendar ids
# are email-like (``…@group.calendar.google.com``) and never contain ``:``, so splitting on
# the *first* separator is unambiguous.
_TOKEN_SEP = ":"


def encode_collection_token(ref: CollectionRef) -> str:
    """A stable ``account[:collection]`` token for a write target (the form's option value)."""
    return f"{ref.account}{_TOKEN_SEP}{ref.collection}" if ref.collection else ref.account


def decode_collection_token(token: str) -> CollectionRef:
    """Parse an ``account[:collection]`` token back into a :class:`CollectionRef`."""
    account, sep, collection = token.partition(_TOKEN_SEP)
    return CollectionRef(account=account, collection=collection if sep else "")


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
            token = encode_collection_token(ref)
            try:
                for event in await provider.list_events(
                    tenant_id=tenant_id,
                    time_range=time_range,
                    calendar_id=ref.collection or None,
                ):
                    # Tag every event with the calendar it came from (#378) — the same
                    # account[:collection] token the New-event picker uses — so the shell can
                    # group events by calendar and toggle each calendar's visibility.
                    event.calendar_id = token
                    events.append(event)
            except Exception as exc:
                log.warning(
                    "calendar read failed; skipping this source (#209)",
                    account=ref.account,
                    collection=ref.collection,
                    error=str(exc),
                )
        events.sort(key=lambda e: e.start)
        return events

    def _search_refs(self, prefs: CollectionPrefs) -> list[CollectionRef]:
        """Ordered, de-duplicated places to find a single event by id.

        Active collection first, then the rest of the enabled set, then the local store —
        a referenced event resolves (and is edited/deleted) wherever it lives.
        """
        refs: list[CollectionRef] = []
        if prefs.active is not None:
            refs.append(prefs.active)
        refs.extend(prefs.enabled)
        refs.append(_LOCAL_REF)
        ordered: list[CollectionRef] = []
        seen: set[tuple[str, str | None]] = set()
        for ref in refs:
            key = (ref.account, ref.collection)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(ref)
        return ordered

    async def get_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> Event | None:
        prefs = await self._load_prefs()
        for ref in self._search_refs(prefs):
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
        all_day: bool = False,
    ) -> Event:
        # Unlike a plain provider (where ``calendar_id`` is a bare collection id), the
        # router reads it as an ``account[:collection]`` token the create form supplies so
        # the operator can pick the target calendar; absent a token it falls back to the
        # write default (see ``_active_ref``). The sub-provider still receives only the
        # bare collection id.
        ref = (
            decode_collection_token(calendar_id)
            if calendar_id
            else await self._active_ref(tenant_id=tenant_id)
        )
        provider = self._provider_for(ref.account) or self._local
        return await provider.create_event(
            tenant_id=tenant_id,
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            calendar_id=ref.collection or None,
            all_day=all_day,
        )

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
    ) -> Event | None:
        # Edit the event wherever it lives: try the active collection, then the rest of
        # the enabled set, then local — the first source that has it wins (#208).
        prefs = await self._load_prefs()
        for ref in self._search_refs(prefs):
            provider = self._provider_for(ref.account)
            if provider is None:
                continue
            try:
                event = await provider.update_event(
                    tenant_id=tenant_id,
                    event_id=event_id,
                    title=title,
                    start=start,
                    end=end,
                    description=description,
                    location=location,
                    calendar_id=ref.collection or None,
                    all_day=all_day,
                )
            except Exception as exc:
                log.warning(
                    "calendar update failed; trying next source (#209)",
                    account=ref.account,
                    collection=ref.collection,
                    error=str(exc),
                )
                continue
            if event is not None:
                return event
        return None

    async def delete_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> bool:
        # Delete the event wherever it lives (#208).
        prefs = await self._load_prefs()
        for ref in self._search_refs(prefs):
            provider = self._provider_for(ref.account)
            if provider is None:
                continue
            try:
                if await provider.delete_event(
                    tenant_id=tenant_id, event_id=event_id, calendar_id=ref.collection or None
                ):
                    return True
            except Exception as exc:
                log.warning(
                    "calendar delete failed; trying next source (#209)",
                    account=ref.account,
                    collection=ref.collection,
                    error=str(exc),
                )
                continue
        return False

    async def find_free_slots(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> list[DateTimeRange]:
        # Free/busy is computed for the write-default calendar — the one a new event
        # lands on (see ``_active_ref``).
        ref = await self._active_ref(tenant_id=tenant_id)
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

    async def _active_ref(self, *, tenant_id: str) -> CollectionRef:
        """The calendar new writes land on: the operator's choice, else a connected one.

        With no explicit ``active`` set, connected beats local (#433): the first enabled
        external calendar wins (it is one the operator is looking at), then a connected
        external provider's own default calendar (Google resolves an empty collection to
        ``primary``), and only when nothing external is connected does the silent local
        store take the write. An explicit ``active`` always wins.
        """
        prefs = await self._load_prefs()
        if prefs.active is not None:
            return prefs.active
        for ref in prefs.enabled:
            if ref.account != LOCAL_ACCOUNT and self._provider_for(ref.account) is not None:
                return ref
        for account, provider in self._external.items():
            try:
                if await provider.is_available(tenant_id=tenant_id):
                    return CollectionRef(account=account)
            except Exception:  # a flaky provider must not break the write — fall through
                continue
        return _LOCAL_REF

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
