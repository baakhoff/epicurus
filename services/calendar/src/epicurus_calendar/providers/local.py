"""Local calendar provider — event store backed by Postgres.

Operates with no external account.  Events are tenant-scoped rows in the
``calendar_events`` table managed by ``LocalEventStore``.  Free-slot
calculation is done in-process by scanning the stored events.

Recurring events (#432) are expanded in Python: a series is one stored row (the
*master*, carrying an RRULE) plus zero or more *exception* rows overriding a single
occurrence (edited or deleted). See ``db.py`` for the storage model and
``epicurus_calendar.providers.base.EditScope`` for what ``"this"``/``"all"`` mean.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from dateutil.rrule import rrule, rrulestr

from epicurus_calendar.db import LocalEventStore, instance_id, parse_instance_id
from epicurus_calendar.models import Attendee, DateTimeRange, Event
from epicurus_calendar.providers.base import CalendarProvider, EditScope
from epicurus_core import Collection, get_logger

log = get_logger("epicurus_calendar.local")


def _safe_rrule(master: Event) -> rrule | None:
    """Parse a series master's stored RRULE; ``None`` (logged) on a corrupt/legacy value.

    Write-time validation (``service.py``) rejects an unparseable RRULE before it is ever
    stored, so this should never fire in practice — it exists so one bad row degrades to
    "this series is skipped" rather than 500ing the whole calendar read.
    """
    try:
        parsed = rrulestr(f"RRULE:{master.recurrence}", dtstart=master.start)
    except Exception as exc:
        log.warning(
            "stored RRULE failed to parse; skipping series", event_id=master.id, error=str(exc)
        )
        return None
    return parsed if isinstance(parsed, rrule) else None


def _occurrence_exists(rule: rrule, at: datetime) -> bool:
    """Whether *at* is exactly one of *rule*'s computed occurrence starts."""
    return bool(rule.between(at, at, inc=True))


def _synthesize_instance(master: Event, occurrence_start: datetime, duration: timedelta) -> Event:
    """An unmodified occurrence of *master*, as a full :class:`Event` (#432)."""
    return master.model_copy(
        update={
            "id": instance_id(master.id, occurrence_start),
            "start": occurrence_start,
            "end": occurrence_start + duration,
            "recurring_event_id": master.id,
            "original_start": occurrence_start,
            "recurrence": None,  # an instance doesn't carry its own series rule
        }
    )


class LocalCalendarProvider(CalendarProvider):
    """Postgres-backed calendar provider — no external account required.

    The single local store has no notion of multiple collections, so ``calendar_id``
    is accepted (to satisfy the provider contract) but ignored, and
    ``list_collections`` returns nothing — local is the silent default (ADR-0030),
    never a selectable account.
    """

    name = "local"

    def __init__(self, store: LocalEventStore) -> None:
        self._store = store

    async def _expand_series(self, *, tenant_id: str, time_range: DateTimeRange) -> list[Event]:
        """Every occurrence of every recurring series overlapping *time_range* (#432)."""
        masters = await self._store.list_master_events(
            tenant=tenant_id, start=time_range.start, end=time_range.end
        )
        occurrences: list[Event] = []
        for master in masters:
            rule = _safe_rrule(master)
            if rule is None:
                continue
            exceptions = await self._store.list_exceptions(tenant=tenant_id, series_id=master.id)
            by_original_start = {exc.original_start: exc for exc in exceptions}
            duration = master.end - master.start
            # dateutil's ``between(after, before, inc=True)`` is inclusive on *both* ends;
            # ``time_range`` is half-open ``[start, end)`` (DateTimeRange's own contract), so
            # an occurrence landing exactly on ``end`` must be dropped, or a series ticking
            # at the same wall-clock time as the window boundary double-counts one occurrence.
            for occurrence_start in rule.between(time_range.start, time_range.end, inc=True):
                if occurrence_start >= time_range.end:
                    continue
                exc = by_original_start.get(occurrence_start)
                if exc is not None:
                    if not exc.excluded:
                        occurrences.append(exc.event)
                    continue
                occurrences.append(_synthesize_instance(master, occurrence_start, duration))
        return occurrences

    async def list_events(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        calendar_id: str | None = None,
    ) -> list[Event]:
        plain = await self._store.list_events(
            tenant=tenant_id,
            start=time_range.start,
            end=time_range.end,
        )
        recurring = await self._expand_series(tenant_id=tenant_id, time_range=time_range)
        return sorted([*plain, *recurring], key=lambda e: e.start)

    async def get_event(
        self, *, tenant_id: str, event_id: str, calendar_id: str | None = None
    ) -> Event | None:
        parsed = parse_instance_id(event_id)
        if parsed is None:
            return await self._store.get_event(tenant=tenant_id, event_id=event_id)
        series_id, original_start = parsed
        master = await self._store.get_event(tenant=tenant_id, event_id=series_id)
        if master is None or not master.recurrence:
            return None
        exceptions = await self._store.list_exceptions(tenant=tenant_id, series_id=series_id)
        exc = next((e for e in exceptions if e.original_start == original_start), None)
        if exc is not None:
            return None if exc.excluded else exc.event
        rule = _safe_rrule(master)
        if rule is None or not _occurrence_exists(rule, original_start):
            return None  # not a real occurrence of this series — a stale/forged id
        return _synthesize_instance(master, original_start, master.end - master.start)

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
    ) -> Event:
        return await self._store.create_event(
            tenant=tenant_id,
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            all_day=all_day,
            recurrence=recurrence,
            attendees=attendees,
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
        recurrence: str | None = None,
        attendees: list[Attendee] | None = None,
        edit_scope: EditScope = "this",
    ) -> Event | None:
        parsed = parse_instance_id(event_id)
        if parsed is None or edit_scope == "all":
            # A plain/non-recurring event id (scope is moot), a series id with scope="all",
            # or an instance id with scope="all" (resolved to its series id below) — either
            # way, edit that row directly rather than creating a per-occurrence exception.
            target_id = parsed[0] if parsed is not None and edit_scope == "all" else event_id
            return await self._store.update_event(
                tenant=tenant_id,
                event_id=target_id,
                title=title,
                start=start,
                end=end,
                description=description,
                location=location,
                all_day=all_day,
                recurrence=recurrence,
                attendees=attendees,
            )
        # edit_scope == "this" on an instance id: override just that occurrence.
        if recurrence is not None:
            raise ValueError(
                "cannot set a recurrence rule on a single occurrence (edit_scope='this'); "
                "use edit_scope='all' to change the whole series"
            )
        series_id, original_start = parsed
        master = await self._store.get_event(tenant=tenant_id, event_id=series_id)
        if master is None or not master.recurrence:
            return None
        rule = _safe_rrule(master)
        if rule is None or not _occurrence_exists(rule, original_start):
            return None
        exceptions = await self._store.list_exceptions(tenant=tenant_id, series_id=series_id)
        existing = next((e for e in exceptions if e.original_start == original_start), None)
        base = (
            existing.event
            if existing is not None
            else _synthesize_instance(master, original_start, master.end - master.start)
        )
        return await self._store.upsert_exception(
            tenant=tenant_id,
            series_id=series_id,
            original_start=original_start,
            title=title if title is not None else base.title,
            start=start if start is not None else base.start,
            end=end if end is not None else base.end,
            description=description if description is not None else base.description,
            location=location if location is not None else base.location,
            all_day=all_day if all_day is not None else base.all_day,
            excluded=False,
            attendees=attendees if attendees is not None else base.attendees,
        )

    async def delete_event(
        self,
        *,
        tenant_id: str,
        event_id: str,
        calendar_id: str | None = None,
        edit_scope: EditScope = "this",
    ) -> bool:
        parsed = parse_instance_id(event_id)
        if parsed is None or edit_scope == "all":
            target_id = parsed[0] if parsed is not None and edit_scope == "all" else event_id
            deleted = await self._store.delete_event(tenant=tenant_id, event_id=target_id)
            if deleted:
                await self._store.delete_exceptions_for(tenant=tenant_id, series_id=target_id)
            return deleted
        series_id, original_start = parsed
        master = await self._store.get_event(tenant=tenant_id, event_id=series_id)
        if master is None or not master.recurrence:
            return False
        rule = _safe_rrule(master)
        if rule is None or not _occurrence_exists(rule, original_start):
            return False
        await self._store.upsert_exception(
            tenant=tenant_id,
            series_id=series_id,
            original_start=original_start,
            title="",
            start=original_start,
            end=original_start,
            description=None,
            location=None,
            all_day=False,
            excluded=True,
        )
        return True

    async def find_free_slots(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> list[DateTimeRange]:
        """Return contiguous gaps of at least *duration_minutes* in *time_range*.

        Reads through :meth:`list_events` (not the store directly) so recurring
        occurrences count as busy time too (#432) — a weekly standup must block its slot
        on every occurrence, not just once on the series' own stored row.
        """
        events = await self.list_events(tenant_id=tenant_id, time_range=time_range)
        return _compute_free_slots(
            busy=[(e.start, e.end) for e in events],
            window_start=time_range.start,
            window_end=time_range.end,
            min_duration=timedelta(minutes=duration_minutes),
        )

    async def is_available(self, *, tenant_id: str) -> bool:
        return True

    async def list_collections(self, *, tenant_id: str) -> list[Collection]:
        # Local is the silent default, not a selectable account (ADR-0030).
        return []


def _compute_free_slots(
    *,
    busy: list[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
    min_duration: timedelta,
) -> list[DateTimeRange]:
    """Find free slots in [window_start, window_end) given busy intervals."""
    merged = _merge_intervals(sorted(busy, key=lambda x: x[0]))
    free: list[DateTimeRange] = []
    cursor = window_start
    for b_start, b_end in merged:
        gap_end = min(b_start, window_end)
        if gap_end - cursor >= min_duration:
            free.append(DateTimeRange(start=cursor, end=gap_end))
        cursor = max(cursor, b_end)
    if window_end - cursor >= min_duration:
        free.append(DateTimeRange(start=cursor, end=window_end))
    return free


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping or adjacent datetime intervals."""
    if not intervals:
        return []
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged
