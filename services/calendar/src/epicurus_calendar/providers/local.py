"""Local calendar provider — event store backed by Postgres.

Operates with no external account.  Events are tenant-scoped rows in the
``calendar_events`` table managed by ``LocalEventStore``.  Free-slot
calculation is done in-process by scanning the stored events.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from epicurus_calendar.db import LocalEventStore
from epicurus_calendar.models import DateTimeRange, Event
from epicurus_calendar.providers.base import CalendarProvider


class LocalCalendarProvider(CalendarProvider):
    """Postgres-backed calendar provider — no external account required."""

    name = "local"

    def __init__(self, store: LocalEventStore) -> None:
        self._store = store

    async def list_events(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
    ) -> list[Event]:
        return await self._store.list_events(
            tenant=tenant_id,
            start=time_range.start,
            end=time_range.end,
        )

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
        return await self._store.create_event(
            tenant=tenant_id,
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
        )

    async def find_free_slots(
        self,
        *,
        tenant_id: str,
        time_range: DateTimeRange,
        duration_minutes: int,
    ) -> list[DateTimeRange]:
        """Return contiguous gaps of at least *duration_minutes* in *time_range*.

        Fetches all events that overlap the range, merges overlapping busy
        intervals, then collects gaps between them that are long enough.
        """
        events = await self._store.list_events(
            tenant=tenant_id,
            start=time_range.start,
            end=time_range.end,
        )
        return _compute_free_slots(
            busy=[(e.start, e.end) for e in events],
            window_start=time_range.start,
            window_end=time_range.end,
            min_duration=timedelta(minutes=duration_minutes),
        )

    async def is_available(self, *, tenant_id: str) -> bool:
        return True


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
