"""The turn-activity record — the agent's *process* for one turn (thinking + tool steps).

Persisted alongside the assistant message (ADR-0041) so the web shell can show the same
**folded** activity timeline when a past conversation is reopened, not only while the turn
streams. Kept in its own module — with no agent or memory imports — so both the agent loop
that *produces* activity and the conversation store that *persists* it can depend on it
without an import cycle.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolStep(BaseModel):
    """One tool call the agent made this turn, as shown in the process timeline (#121)."""

    tool: str
    # The settled status; "running" is a live-only stream state, never persisted.
    status: str  # "ok" | "error"
    # The call's arguments as compact JSON, for the expandable step detail. None when empty.
    detail: str | None = None


class MessageActivity(BaseModel):
    """The agent's process for one assistant turn: its thinking and its tool steps.

    An empty record is the norm — a plain answer from a non-reasoning model with no tool
    use — so the turn persists ``None`` rather than an empty record in that case (see
    :meth:`is_empty`), keeping old rows and trivial turns free of an activity blob.
    """

    # The model's reasoning / chain-of-thought for the turn, concatenated across rounds.
    thinking: str = ""
    steps: list[ToolStep] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """True when there is nothing worth persisting (no thinking and no tool steps)."""
        return not self.thinking and not self.steps
