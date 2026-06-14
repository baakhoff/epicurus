"""The shared chat contract — the single source of truth for chat message and
result shapes used across the platform (ADR-0021).

The core's LLM gateway, its module-facing platform API, and the typed
``PlatformClient`` that modules call all speak the same two shapes:

* :class:`ChatMessage` — one message in a chat exchange (request side).
* :class:`ChatResult` — a non-streaming chat completion (response side).

Both live here so there is exactly one definition; ``epicurus_core_app`` imports
them rather than redefining its own. ``PlatformMessage`` and
``PlatformChatResponse`` are retained as backward-compatible aliases so existing
module code (``from epicurus_core import PlatformMessage``) keeps working.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class EntityRef(BaseModel):
    """A reference to a module entity the assistant mentions (ADR-0019).

    The UI renders it as an interactive chip: hover → a core hover-card, click → open
    in the right panel. This carries enough to render the chip immediately; the richer
    hover-card is fetched on demand from the module's resolver.
    """

    ref_id: str
    module: str
    kind: str
    title: str
    summary: str | None = None


class HoverCardDetail(BaseModel):
    """One label/value row of a hover-card (e.g. ``{"deadline", "tomorrow 9am"}``)."""

    label: str
    value: str


class HoverCardLink(BaseModel):
    """An outbound link a hover-card may carry (e.g. to a GitHub-issue module)."""

    label: str
    url: str


class HoverCard(BaseModel):
    """The uniform hover-card / entity-detail envelope a module resolver returns.

    Core-owned and identical for every entity (ADR-0018/ADR-0019): the inline
    hover-card and the panel's ``entity-detail`` view both render this one shape.
    """

    title: str
    description: str = ""
    details: list[HoverCardDetail] = Field(default_factory=list)
    href: HoverCardLink | None = None


class ToolEnvelope(BaseModel):
    """A tool's output enriched with entity references (ADR-0019).

    A module tool may return this (JSON-serialized — see :func:`tool_envelope`) instead
    of a bare string. The agent feeds ``text`` back to the model and lifts
    ``entity_refs`` onto the turn, where the UI renders them as chips.
    """

    text: str
    entity_refs: list[EntityRef] = Field(default_factory=list)


def tool_envelope(text: str, entity_refs: list[EntityRef] | None = None) -> str:
    """Serialize a tool result that carries entity references (ADR-0019).

    A module's MCP tool returns the result of this helper so the agent can surface the
    referenced entities as chips while still feeding ``text`` back to the model.
    """

    return ToolEnvelope(text=text, entity_refs=entity_refs or []).model_dump_json()


AttachmentSource = Literal["module", "file", "chat"]


class Attachment(BaseModel):
    """A piece of context the user attaches to a message (ADR-0019).

    ``source`` says where it comes from: an uploaded ``file`` (its bytes held core-side
    under ``att_id``), another ``chat`` (``ref_id`` = that session id), or a ``module``
    entity (``module`` + ``ref_id``, resolved through the module's attachment surface).
    The agent expands attachments into the turn's context; like ``entity_refs`` they are
    UI/agent metadata and never reach a provider as a message field.
    """

    att_id: str
    source: AttachmentSource
    kind: str = ""
    ref_id: str | None = None
    title: str = ""
    # The owning module, for ``source == "module"`` attachments (routing the resolve).
    module: str | None = None


class ChatMessage(BaseModel):
    """One message in a chat exchange.

    ``content`` is optional: an assistant tool-call turn carries ``tool_calls`` with no
    content, and a ``tool`` result carries ``tool_call_id`` + ``name``. ``entity_refs``
    (assistant-emitted, ADR-0019) is UI metadata — it rides alongside the message but is
    stripped before the message reaches a provider (see :meth:`provider_dump`).
    """

    role: Role
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    # UI/agent-only (ADR-0019); optional so they drop out of the default provider payload.
    entity_refs: list[EntityRef] | None = None
    attachments: list[Attachment] | None = None

    def provider_dump(self) -> dict[str, Any]:
        """Serialize for an LLM provider call — UI/agent-only fields removed (ADR-0019)."""
        return self.model_dump(exclude_none=True, exclude={"entity_refs", "attachments"})


class ChatResult(BaseModel):
    """A non-streaming chat completion."""

    model: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


# ── Backward-compatible aliases ──────────────────────────────────────────────────
# Modules historically imported these names from ``epicurus_core``. They are the
# same shapes as the canonical contract above — kept as aliases, not duplicate
# definitions, so there is still one source of truth.
PlatformMessage = ChatMessage
PlatformChatResponse = ChatResult
