"""Shared ``review``-archetype wire contract (ADR-0033, ADR-0090).

Knowledge and notes each shipped their own byte-identical copies of these shapes
(``#KB-refactor``) — a new review-page adopter (governed playbooks, #525) would have been
the third. This module is the single source of truth so every module implementing a
``review`` page (:data:`epicurus_core.manifest.PageArchetype`) gets the same
edit-before-approve contract for free, with no per-module special case.

Only the *wire contract* lives here — persistence stays owned by each module's own
Postgres, mirroring the editor version-history precedent (ADR-0046): shared code for the
shape, per-module tables for storage.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AutomationPreview(BaseModel):
    """A human-readable rendering of a proposed automation, for the review modal (#667).

    The small additive extension the automations review page needs (ADR-0107): a text diff
    reads badly for a structured object, so an automation suggestion carries this alongside
    the shared ``ReviewSuggestion`` fields and the shell renders the automation *understandably*
    — trigger in words, filter, what the agent will do, autonomy, sinks — plus a **model
    picker** the operator can change before approving. Every non-automation review page leaves
    :attr:`ReviewSuggestion.automation` ``None`` and this is never constructed.

    ``model`` is the drafted per-automation model (``None`` = the tenant's default chat model);
    it is the one field editable before approval, sent back as the approve ``content``.
    """

    name: str
    trigger: str  # "When mail arrives from …" / "Every Monday at 09:00"
    filter: str = ""  # "importance = high", or "" when the trigger has no filter
    action: str  # what the agent is asked to do (the automation's prompt)
    autonomy: str  # notify | propose | act | silent_act
    autonomy_label: str  # the level in words, e.g. "Notify — look, don't touch"
    sinks: list[str] = Field(default_factory=list)
    model: str | None = None  # the drafted model; None = the tenant's default


class ReviewSuggestion(BaseModel):
    """One pending change in a review queue, with a server-computed unified diff."""

    id: str
    title: str
    path: str
    operation: str
    origin: str
    note: str = ""
    created_at: str  # ISO-8601
    diff: str = ""  # unified diff (current -> proposed); empty for non-content ops
    to_path: str = ""  # destination for a move (empty otherwise)
    # Full texts so the shell can render an editable draft (ADR-0090): ``current`` is the
    # live document (empty for a create), ``content`` is the proposal (empty for a delete).
    current: str = ""
    content: str = ""
    # Present only for an automation proposal (#667/ADR-0107): the structured, human-readable
    # rendering the modal shows instead of a raw text diff, with the pre-approval model picker.
    # ``None`` for every document-shaped suggestion (knowledge, notes, playbooks).
    automation: AutomationPreview | None = None


class ReviewData(BaseModel):
    """The ``review`` archetype's data: the queue of pending suggestions (ADR-0018)."""

    title: str = "Suggestions"
    suggestions: list[ReviewSuggestion] = Field(default_factory=list)


class ApplyResult(BaseModel):
    """The outcome of approving or rejecting a suggestion."""

    id: str
    status: str  # "approved" | "rejected"
    path: str
    operation: str
    indexed: bool = False


class ApproveBody(BaseModel):
    """Optional approve payload: the operator's edited content (ADR-0090) — a free-form
    edit, a per-hunk merge, or both layered together. Absent means apply the module's
    proposal unedited."""

    content: str | None = None


class ReviewDecision(BaseModel):
    """An audit record of one resolved suggestion (ADR-0090): what was proposed vs. what
    was actually applied. The pending queue drops a row on resolution (ADR-0033 — "the
    queue *is* the set of rows"); this is the durable trail that replaces it, so an
    operator's edit is a delta worth keeping, not just a mutation of a row that vanishes."""

    id: str
    title: str
    path: str
    operation: str
    origin: str
    note: str = ""
    created_at: str  # when the suggestion was originally proposed
    decided_at: str  # when the operator resolved it
    decision: str  # "approved" | "rejected"
    proposed_content: str = ""
    applied_content: str = ""  # empty for a reject, or an operation with no content
    to_path: str = ""


class ReviewAuditData(BaseModel):
    """The resolved-decision audit trail for a review queue, newest first (ADR-0090)."""

    decisions: list[ReviewDecision] = Field(default_factory=list)


__all__ = [
    "ApplyResult",
    "ApproveBody",
    "AutomationPreview",
    "ReviewAuditData",
    "ReviewData",
    "ReviewDecision",
    "ReviewSuggestion",
]
