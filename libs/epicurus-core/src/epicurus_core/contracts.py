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


class DraftReview(BaseModel):
    """An outbound artifact a module composed for the operator to approve before it is sent
    (ADR-0085).

    A module's *compose* tool (mail's ``mail_send`` / ``mail_reply``, and any future outbound
    channel such as a Phase-4 chat bridge) returns this — via :func:`draft_review` — instead of
    transmitting. The agent loop recognizes it the way it recognizes a :class:`ToolEnvelope`, and
    **suspends the turn**: the shell renders ``draft`` in a review pane, and only the operator's
    Confirm makes the core POST ``draft`` back to the module's transmit endpoint (``POST /send``).
    The MCP surface therefore exposes *no* tool that transmits — the model can compose but can
    never send. On Decline nothing is sent and the model is told so (with an optional reason), so
    it can revise. The guarantee is the contract, not a prompt.

    ``kind`` is the render/channel discriminator the shell switches on (``"mail"`` selects the
    email-reader pane). ``module`` names the module whose transmit endpoint sends ``draft``.
    ``summary`` is a one-line human/log label (e.g. ``"Email to bob@… — Re: Lunch"``). ``draft``
    is an opaque, channel-specific dict that is **both** what the pane shows and what the transmit
    endpoint sends, so what the operator approves is byte-for-byte what goes out.
    """

    kind: str
    module: str
    summary: str = ""
    draft: dict[str, Any] = Field(default_factory=dict)


def draft_review(*, kind: str, module: str, draft: dict[str, Any], summary: str = "") -> str:
    """Serialize a compose tool's pending-draft result (ADR-0085) — see :class:`DraftReview`.

    A module returns the result of this from a compose tool (``mail_send`` / ``mail_reply``) so
    the core suspends the turn for the operator to Confirm/Decline, rather than sending inline.
    """

    return DraftReview(kind=kind, module=module, summary=summary, draft=draft).model_dump_json()


# A large list-style result inflates model context two ways: a module's own envelope text
# (one line per item) and the entity-ref id block the core appends to it (ADR-0079). Both
# share this one default cap so they never disagree about how much of a big result was
# actually shown (#468) — the id block is capped in the agent loop; `capped_listing` below
# lets a module cap its own text the same way, with one call instead of reinventing it.
LIST_CAP = 50


def capped_listing(items: list[str], *, limit: int = LIST_CAP, noun: str = "item") -> str:
    """A "Found N {noun}(s):\\n- ...\\n- ..." listing, capped to *limit* lines (#468).

    *items* are pre-formatted lines (e.g. ``f"- {e.title} ({when})"``); past *limit* a
    trailing note says how many were left out, so the model isn't told a result was
    exhaustive when it wasn't.
    """
    shown = items[:limit]
    body = "\n".join(shown)
    if len(items) > limit:
        body += f"\n… and {len(items) - limit} more — narrow the range or ask for more."
    return f"Found {len(items)} {noun}(s):\n{body}"


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

    ``content`` is plain text for every message the agent builds itself; it only takes the
    OpenAI-style content-parts array (``[{"type": "text", ...}, {"type": "image_url", ...}]``)
    transiently, on the assembled turn sent to the gateway, when an image attachment resolves
    for a vision-capable model (#633) — never on a persisted message (history/memory stay
    text-only, so a stored turn never balloons with base64 image data).
    """

    role: Role
    content: str | list[dict[str, Any]] | None = None
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
    # The model's reasoning / chain-of-thought, when it exposes one (a reasoning model, or a
    # local model that inlines ``<think>…</think>``). Surfaced in the activity timeline and
    # kept out of ``content`` so the answer stays clean (ADR-0041).
    reasoning: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


# ── Backward-compatible aliases ──────────────────────────────────────────────────
# Modules historically imported these names from ``epicurus_core``. They are the
# same shapes as the canonical contract above — kept as aliases, not duplicate
# definitions, so there is still one source of truth.
PlatformMessage = ChatMessage
PlatformChatResponse = ChatResult


# ── Account / collection model (ADR-0030) ────────────────────────────────────────
# A domain module (calendar, tasks) backs itself with a silent ``local`` store and,
# when connected, 0+ external accounts (Google). Each account exposes collections
# (calendars / task-lists) the operator toggles on/off and switches between. ``local``
# is the default and is never surfaced as a selectable account.

LOCAL_ACCOUNT = "local"
"""The implicit, zero-config default account every domain module always has (ADR-0030).

Never returned from a module's ``/accounts`` and never shown in the shell — it is the
fallback used for reads and writes when no external collection is enabled or active.
"""


class CollectionRef(BaseModel):
    """A pointer to one collection within an account (ADR-0030).

    ``account`` is a provider/account id (e.g. ``"google"``) or :data:`LOCAL_ACCOUNT`;
    ``collection`` is the id within it (a Google calendar id / task-list id), empty for
    the local default. Refs compare by value, so they round-trip through stored prefs.
    """

    account: str
    collection: str = ""


class Collection(BaseModel):
    """A collection a connected account exposes — a calendar or a task-list (ADR-0030).

    A module returns these from ``/accounts`` (discovery) with ``enabled`` / ``active``
    unset; the core fills them when it merges the operator's stored prefs for the shell.
    ``writable`` is False for a read-only collection (e.g. a subscribed Google calendar)
    so the shell can keep it out of the active/write picker.
    """

    account: str
    collection: str
    title: str
    writable: bool = True
    # Optional presentation colour (any CSS colour string — e.g. the calendar's Google
    # backgroundColor). The shell prefers it over a derived hue so events and toggles
    # match the user's own colours (#431); None means "derive one".
    color: str | None = None
    # Filled by the core's merged view (GET …/collections); left unset in module discovery.
    enabled: bool | None = None
    active: bool | None = None

    def ref(self) -> CollectionRef:
        """The :class:`CollectionRef` addressing this collection."""
        return CollectionRef(account=self.account, collection=self.collection)


class Account(BaseModel):
    """One external account a domain module can draw collections from (ADR-0030).

    ``connected`` reflects the live OAuth state; ``collections`` is populated only when
    connected. ``local`` is never represented as an Account.
    """

    account: str
    provider: str
    label: str
    connected: bool = False
    collections: list[Collection] = Field(default_factory=list)


class AccountsView(BaseModel):
    """A module's ``GET /accounts`` response — its connected accounts + collections (ADR-0030).

    ``noun`` / ``multi`` echo the module's :class:`~epicurus_core.manifest.CollectionsSpec`
    so the shell can label and shape the picker; the core re-uses this same shape for the
    merged ``GET …/collections`` view, filling each collection's ``enabled`` / ``active``.
    """

    noun: str
    multi: bool
    accounts: list[Account] = Field(default_factory=list)


class CollectionPrefs(BaseModel):
    """The operator's stored selection for a module (ADR-0030).

    ``enabled`` are the external collections turned on; ``active`` is the single write
    target / single-view source. Both default to "use ``local``": an empty ``enabled``
    means reads fall back to local, and a null ``active`` means writes go to local.
    """

    enabled: list[CollectionRef] = Field(default_factory=list)
    active: CollectionRef | None = None
