"""Provider-neutral Task domain model (ADR-0016)."""

from __future__ import annotations

from pydantic import BaseModel


class Task(BaseModel):
    """A task in the provider-neutral domain model."""

    id: str
    title: str
    notes: str | None = None
    # ISO date string, e.g. "2025-01-15" or RFC 3339, e.g. "2025-01-15T00:00:00.000Z"
    due: str | None = None
    completed: bool = False
    completed_at: str | None = None
