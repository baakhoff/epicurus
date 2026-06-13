"""Provider-neutral calendar domain model.

The domain model is independent of any provider.  A Google event and a local
event are both ``Event`` objects — callers never see provider-specific shapes.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


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
