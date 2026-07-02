"""Provider-neutral calendar domain model.

The domain model is independent of any provider.  A Google event and a local
event are both ``Event`` objects — callers never see provider-specific shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator


class DateTimeRange(BaseModel):
    """A half-open ``[start, end)`` interval, both values timezone-aware."""

    start: datetime
    end: datetime


class Attendee(BaseModel):
    """A guest invited to an event (#432).

    ``response_status`` uses Google's vocabulary — ``needsAction`` / ``accepted`` /
    ``declined`` / ``tentative`` — which is also iCalendar's PARTSTAT set (RFC 5545), so
    it doubles as the natural provider-neutral choice rather than a Google-only one.
    """

    email: str
    display_name: str | None = None
    response_status: str = "needsAction"


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
    # Recurrence (#432): an RFC 5545 RRULE string (no leading ``"RRULE:"``), e.g.
    # ``"FREQ=WEEKLY;COUNT=10"``. Set on a recurring *series* (the master); ``None`` for a
    # one-off event or an expanded *instance* (an instance's pattern lives on its series).
    recurrence: str | None = None
    # For an *instance* of a recurring series (an expanded occurrence, or an edited/deleted
    # exception to one), the series' event id; ``None`` for a one-off event or the series
    # itself. Mirrors Google's ``recurringEventId``.
    recurring_event_id: str | None = None
    # For an instance whose own start has moved away from its series' computed schedule
    # (a single occurrence rescheduled), the *original* unmodified occurrence start it
    # replaces — the key a provider uses to find/override that occurrence. Mirrors
    # Google's ``originalStartTime``. ``None`` for a one-off event, a series itself, or an
    # unmodified instance (whose current start already equals its scheduled slot).
    original_start: datetime | None = None
    # Guests invited to the event (#432); empty for none. Google-backed events reflect the
    # live RSVP status per guest; a newly invited local guest starts ``needsAction``.
    attendees: list[Attendee] = Field(default_factory=list)

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

    @field_validator("original_start")
    @classmethod
    def _ensure_aware_optional(cls, value: datetime | None) -> datetime | None:
        """Same naive-to-UTC coercion as start/end, for the optional ``original_start``."""
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
