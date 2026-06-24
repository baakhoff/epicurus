"""The turn-activity record — the agent's *process* for one turn (thinking + tool steps).

Persisted alongside the assistant message (ADR-0041) so the web shell can show the same
**folded** activity timeline when a past conversation is reopened, not only while the turn
streams. Kept in its own module — with no agent or memory imports — so both the agent loop
that *produces* activity and the conversation store that *persists* it can depend on it
without an import cycle.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# A single thinking block can be long; cap each one so the persisted timeline stays bounded.
_THINKING_ITEM_CAP = 20_000


class ToolStep(BaseModel):
    """One tool call the agent made this turn, as shown in the process timeline (#121)."""

    tool: str
    # The settled status; "running" is a live-only stream state, never persisted.
    status: str  # "ok" | "error"
    # The call's arguments as compact JSON, for the expandable step detail. None when empty.
    detail: str | None = None


class ThinkingItem(BaseModel):
    """A run of the model's reasoning, in chronological position on the timeline."""

    kind: Literal["thinking"] = "thinking"
    text: str = ""


class ToolItem(BaseModel):
    """A settled tool call, in chronological position on the timeline."""

    kind: Literal["tool"] = "tool"
    tool: str
    status: str  # "ok" | "error"
    detail: str | None = None


# Ordered timeline entry: thinking or a tool step, discriminated by ``kind`` (#300).
ActivityItem = Annotated[ThinkingItem | ToolItem, Field(discriminator="kind")]


class MessageActivity(BaseModel):
    """The agent's process for one assistant turn: its thinking and its tool steps.

    An empty record is the norm — a plain answer from a non-reasoning model with no tool
    use — so the turn persists ``None`` rather than an empty record in that case (see
    :meth:`is_empty`), keeping old rows and trivial turns free of an activity blob.

    ``timeline`` carries the **chronological interleaving** (think → call → think); the flat
    ``thinking``/``steps`` are derived from it and kept for backward compatibility (older rows
    have them but no ``timeline``, and the web falls back to them then).
    """

    # The model's reasoning / chain-of-thought for the turn, concatenated across rounds.
    thinking: str = ""
    steps: list[ToolStep] = Field(default_factory=list)
    timeline: list[ActivityItem] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """True when there is nothing worth persisting (no thinking and no tool steps)."""
        return not self.thinking and not self.steps


def append_thinking(timeline: list[ActivityItem], text: str) -> None:
    """Append reasoning text, coalescing it into the trailing thinking block (capped).

    Consecutive reasoning within a round becomes one block; a tool step between two runs of
    reasoning splits them into two blocks — that's what makes the timeline read in order.
    """
    if not text:
        return
    last = timeline[-1] if timeline else None
    if isinstance(last, ThinkingItem):
        last.text = (last.text + text)[:_THINKING_ITEM_CAP]
    else:
        timeline.append(ThinkingItem(text=text[:_THINKING_ITEM_CAP]))


def append_tool(timeline: list[ActivityItem], tool: str, status: str, detail: str | None) -> None:
    """Append a settled tool step to the timeline (closes any open thinking block)."""
    timeline.append(ToolItem(tool=tool, status=status, detail=detail))


def activity_from_timeline(timeline: list[ActivityItem], *, thinking_cap: int) -> MessageActivity:
    """Build a `MessageActivity`, deriving the flat `thinking`/`steps` from the ordered timeline."""
    thinking = "".join(i.text for i in timeline if isinstance(i, ThinkingItem))[:thinking_cap]
    steps = [
        ToolStep(tool=i.tool, status=i.status, detail=i.detail)
        for i in timeline
        if isinstance(i, ToolItem)
    ]
    return MessageActivity(thinking=thinking, steps=steps, timeline=timeline)
