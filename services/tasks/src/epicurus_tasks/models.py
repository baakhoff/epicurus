"""Provider-neutral Task domain model (ADR-0016)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, computed_field

#: Valid priority levels (local-only; Google Tasks has no priority field).
VALID_PRIORITIES: frozenset[str] = frozenset({"low", "medium", "high"})
#: Valid status values. "in_progress" is local-only; Google degrades it to "open".
VALID_STATUSES: frozenset[str] = frozenset({"open", "in_progress", "done"})


class Task(BaseModel):
    """A task in the provider-neutral domain model."""

    id: str
    title: str
    notes: str | None = None
    # ISO date string, e.g. "2025-01-15" or RFC 3339, e.g. "2025-01-15T00:00:00.000Z"
    due: str | None = None
    # "open" / "in_progress" / "done"  ("in_progress" is local-only; Google degrades to "open")
    status: Literal["open", "in_progress", "done"] = "open"
    completed_at: str | None = None
    # "low" / "medium" / "high" — local-only; Google Tasks has no priority field
    priority: Literal["low", "medium", "high"] | None = None
    # Free-form labels; local-only
    tags: list[str] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def completed(self) -> bool:
        """Backward-compat alias: True when status is "done"."""
        return self.status == "done"
