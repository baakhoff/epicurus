"""Provider-neutral calendar domain model.

The domain model is independent of any provider.  A Google event and a local
event are both ``Event`` objects — callers never see provider-specific shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, field_validator


class DateTimeRange(BaseModel):
    """A half-open ``[start, end)`` interval, both values timezone-aware."""

    start: datetime
    end: datetime


class Event(BaseModel):
    """A calendar event — the single, provider-agnostic representation."""

    id: str
    title: str
    start: datetime
    end: datetime
    description: str | None = None
    location: str | None = None
    provider: str
    # The calendar this event belongs to, as the router's ``account[:collection]`` token
    # (e.g. ``local`` or ``google:primary``) — the same token the New-event picker uses, so
    # the shell can group events by calendar and toggle each on/off (#378). Set by the router
    # on read; ``None`` for a bare single-provider event (e.g. in unit tests).
    calendar_id: str | None = None
    # An all-day (date-only) event. When true, ``start``/``end`` are UTC-midnight
    # boundaries of a *floating* date range — ``end`` is **exclusive** (the day after the
    # last day, matching Google's all-day model), and the shell renders them on their
    # calendar date with no timezone conversion. A single-day all-day event spans one day,
    # so ``end == start + 1 day``.
    all_day: bool = False

    @field_validator("start", "end")
    @classmethod
    def _ensure_aware(cls, value: datetime) -> datetime:
        """Coerce a naive datetime to UTC so events from different providers compare.

        The local store round-trips datetimes through a tz-naive DB column, while Google
        returns tz-aware RFC3339 instants. A page that overlays both then sorts a mix of
        naive and aware values, raising ``TypeError: can't compare offset-naive and
        offset-aware datetimes`` (the merge sort in ``CalendarRouter.list_events``).
        Normalising every event datetime to aware (UTC when naive) enforces this model's
        documented timezone-aware contract and keeps cross-provider ordering total.
        """
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
