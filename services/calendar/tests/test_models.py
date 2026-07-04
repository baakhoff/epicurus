"""Unit tests for the calendar domain model."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from epicurus_calendar.models import DateTimeRange, Event


def _dt(hour: int) -> datetime:
    return datetime(2025, 6, 15, hour, 0, 0, tzinfo=UTC)


def test_event_round_trips() -> None:
    event = Event(
        id="abc",
        title="Stand-up",
        start=_dt(9),
        end=_dt(10),
        description="Daily sync",
        location="Zoom",
        provider="local",
    )
    data = event.model_dump()
    assert data["id"] == "abc"
    assert data["title"] == "Stand-up"
    assert data["provider"] == "local"


def test_event_optional_fields_default_none() -> None:
    event = Event(id="x", title="T", start=_dt(9), end=_dt(10), provider="local")
    assert event.description is None
    assert event.location is None


def test_datetime_range() -> None:
    r = DateTimeRange(start=_dt(9), end=_dt(17))
    assert r.start < r.end


def test_event_model_dump_json_serializes_datetimes() -> None:
    event = Event(id="y", title="Meeting", start=_dt(10), end=_dt(11), provider="google")
    dumped = event.model_dump(mode="json")
    assert isinstance(dumped["start"], str)
    assert "T" in dumped["start"]


def test_naive_event_datetimes_are_coerced_to_utc() -> None:
    """A naive start/end (as the tz-naive local DB column yields) becomes UTC-aware."""
    naive_start = datetime(2025, 6, 15, 15, 0, 0)  # no tzinfo
    naive_end = datetime(2025, 6, 15, 16, 0, 0)
    event = Event(id="z", title="Local", start=naive_start, end=naive_end, provider="local")
    assert event.start.tzinfo is not None
    assert event.end.tzinfo is not None
    assert event.start.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    # Wall-clock instant is preserved (naive value read as UTC, not shifted).
    assert event.start.hour == 15


def test_aware_non_utc_event_datetimes_are_normalized_to_utc() -> None:
    """A non-UTC aware start/end/original_start normalizes to a UTC offset (#467).

    Google's RFC3339 timestamps carry the event's own zone offset (e.g. ``-04:00`` for an
    America/New_York meeting), not necessarily ``+00:00`` — construction must re-normalize
    it rather than passing the foreign offset straight through, or a timed field drifts from
    the codebase's ``+00:00``/``Z`` convention on serialization.
    """
    ny = ZoneInfo("America/New_York")
    start = datetime(2025, 6, 15, 9, 0, 0, tzinfo=ny)  # 09:00 EDT == 13:00 UTC
    end = datetime(2025, 6, 15, 10, 0, 0, tzinfo=ny)
    event = Event(
        id="z2", title="Zoned", start=start, end=end, provider="google", original_start=start
    )
    assert event.start.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert event.end.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert event.original_start is not None
    assert event.original_start.utcoffset().total_seconds() == 0
    # The instant is preserved, just re-labeled as UTC rather than shifted.
    assert (event.start.hour, event.end.hour, event.original_start.hour) == (13, 14, 13)


def test_mixed_naive_and_aware_events_sort_without_typeerror() -> None:
    """The regression: overlaying a local (naive) and a Google (aware) calendar.

    ``CalendarRouter.list_events`` sorts the merged events by ``start``; before the model
    coerced naive datetimes this raised ``TypeError: can't compare offset-naive and
    offset-aware datetimes`` and 500'd the calendar page.
    """
    local = Event(
        id="local-1",
        title="Local 15:00",
        start=datetime(2025, 6, 15, 15, 0, 0),  # naive, from the local store
        end=datetime(2025, 6, 15, 16, 0, 0),
        provider="local",
    )
    google = Event(
        id="google-1",
        title="Google 09:00Z",
        start=_dt(9),  # tz-aware, from Google
        end=_dt(10),
        provider="google",
    )
    ordered = sorted([local, google], key=lambda e: e.start)
    assert [e.id for e in ordered] == ["google-1", "local-1"]  # 09:00Z before 15:00Z
