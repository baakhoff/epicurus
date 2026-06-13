"""Unit tests for the calendar domain model."""

from __future__ import annotations

from datetime import UTC, datetime

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
