"""Provider-neutral Task domain model (ADR-0016)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, computed_field

#: Valid priority levels (local-only; Google Tasks has no priority field).
VALID_PRIORITIES: frozenset[str] = frozenset({"low", "medium", "high"})
#: Valid status values. "in_progress" is local-only; Google degrades it to "open".
VALID_STATUSES: frozenset[str] = frozenset({"open", "in_progress", "done"})

#: Which tasks a board / list read includes — distinct from a single Task's ``status``
#: (ADR-0049). ``"open"`` excludes completed (open + in_progress), ``"done"`` is completed
#: only, ``"all"`` is both. Backs the board's *Show* filter and the provider read seam.
TaskScope = Literal["open", "done", "all"]
VALID_TASK_SCOPES: frozenset[str] = frozenset({"open", "done", "all"})


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
    # Repeat rule (#471): a bare RFC 5545 RRULE string (no leading "RRULE:"), e.g.
    # "FREQ=WEEKLY". Set on a *recurring* task; ``None`` for a one-off. Google Tasks has no
    # recurrence field, so this is emulated module-side and stored per provider (a column on
    # the local row; a side table keyed by task id for Google) — see ADR-0082. Completing a
    # task that carries a rule materializes the next instance (the router owns that logic).
    repeat: str | None = None
    # The list (category) the task belongs to — stamped by the router when it aggregates
    # across the operator's enabled lists (ADR-0036). ``list_id`` routes a per-task mutation
    # to the owning list; ``list_title`` is the human label for the board's category tag.
    # Both are None for the local default and for tasks fetched outside the board path.
    list_id: str | None = None
    list_title: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def completed(self) -> bool:
        """Backward-compat alias: True when status is "done"."""
        return self.status == "done"
