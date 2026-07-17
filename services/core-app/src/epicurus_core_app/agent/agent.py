"""The agent orchestrator — a thin tool-calling loop (ADR-0001).

A turn: ask the LLM (offering the modules' tools via the gateway), run any tool calls
through MCP, feed the results back, and loop until the model answers or ``max_steps``
is reached. The agent talks to models only through the gateway and to modules only
through MCP — never a provider SDK. It inherits the gateway's power-state behavior.

``run`` resolves a turn in one response; ``run_stream`` yields the same turn as it
happens — content deltas, tool progress, then the final turn — for the web UI.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from epicurus_core import (
    LIST_CAP,
    Attachment,
    DraftReview,
    EntityRef,
    SideEffect,
    ToolEnvelope,
    WritesDocument,
    get_logger,
)
from epicurus_core_app.agent.activity import (
    ActivityItem,
    MessageActivity,
    activity_from_timeline,
    append_thinking,
    append_tool,
)
from epicurus_core_app.agent.attachments import ExpandedAttachments, ImagePart
from epicurus_core_app.agent.builtins import ASK_USER_TOOL
from epicurus_core_app.agent.instructions import AgentInstructionsStore
from epicurus_core_app.agent.mcp_host import McpHost, ModuleUnreachableError, ToolCallError
from epicurus_core_app.agent.pending_drafts import PendingDraftStore
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.memory.extraction import FactExtractor
from epicurus_core_app.memory.extraction_queue import ExtractionQueue
from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.memory.profile import StandingProfileStore
from epicurus_core_app.readiness import Readiness

log = get_logger("epicurus_core_app.agent")

# Persisted-activity bounds: a reasoning trace can be very long, so cap what is stored, and
# keep a tool step's argument detail short (it's a glanceable hint, not the full payload).
_THINKING_CAP = 20_000
_TOOL_DETAIL_CAP = 500

# How long recall (a single embedding round-trip) may take before the turn proceeds without it.
# Recall is the one memory step still on the response path; a cold or busy embedder must never
# delay the first token (ADR-0051). The best-effort assemble already degrades to no recall.
# 4s (was 2s): on a single GPU the recall embed often forces an Ollama model swap (chat model
# out, embed model in) that 2s could not outlast, so recall was skipped on nearly every turn.
# Operators who keep the embed model warm can lower this; slower hardware can raise it.
_DEFAULT_RECALL_TIMEOUT_S = 4.0

# A reasoning model (qwen3, deepseek-r1, …) sometimes emits its <think> block and then stops —
# no answer text and no tool call. The loop would end the turn with empty content, which renders
# as a silent "stop" (an activity trace with no answer bubble). Nudge it once to commit to an
# answer, then continue the loop (bounded by max_steps); if it still says nothing, fall back to a
# clear message rather than persisting an empty turn.
_ANSWER_NUDGE = "Please continue and give your final answer to my last message."
_EMPTY_ANSWER_FALLBACK = (
    "I wasn't able to produce an answer that time — please try again or rephrase your request."
)

# Vision gating (#633): an image attachment is only ever sent to a model that declares vision
# support (model-caps) — a non-vision model gets this canned explanation instead, before any
# provider call, rather than a mangled attempt or a raw provider 400.
_STOPPED_UNSUPPORTED_MEDIA = "unsupported_media"
_VISION_UNSUPPORTED_MESSAGE = (
    "I can't see images with this model — switch to a vision-capable model to use image "
    "attachments."
)


def _vision_unsupported_turn() -> AgentTurn:
    return AgentTurn(content=_VISION_UNSUPPORTED_MESSAGE, stopped=_STOPPED_UNSUPPORTED_MEDIA)


def _text_only(content: str | list[dict[str, Any]] | None) -> str | None:
    """``content`` as plain text, or ``None`` for the transient multimodal-parts shape.

    Recall, fact extraction, and history persistence only ever run against messages
    *before* :func:`_attach_images` mutates a turn (it operates on a copy of the assembled
    convo, never the persisted/pre-assembly messages) — so this should never actually see a
    list in practice, but the type is now a union and callers narrow explicitly rather than
    assume.
    """
    return content if isinstance(content, str) else None


def _attach_images(convo: list[ChatMessage], images: list[ImagePart]) -> list[ChatMessage]:
    """Rewrite the last user message's content into multimodal parts carrying ``images``.

    Applied to the assembled convo just before the provider call — never to what gets
    persisted (:meth:`Agent._expand_attachments` already kept the persisted messages
    text-only), so a stored turn never balloons with base64 image data. LiteLLM's own
    provider adapters translate this OpenAI-style content-parts shape for us — a local
    ``ollama_chat`` call becomes Ollama's ``images`` field, a hosted call keeps the array —
    so no per-provider branching is needed here.
    """
    for i in range(len(convo) - 1, -1, -1):
        if convo[i].role != "user":
            continue
        text = _text_only(convo[i].content)
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        parts.extend(
            {"type": "image_url", "image_url": {"url": f"data:{img.mime};base64,{img.data_b64}"}}
            for img in images
        )
        return [*convo[:i], convo[i].model_copy(update={"content": parts}), *convo[i + 1 :]]
    return convo


# Mid-stream failure handling (#453). When a streaming turn dies part-way — most often the local
# model stopping mid-answer as it loads another model / evaluates a long prompt and the socket
# read aborts — we keep the partial answer + activity instead of discarding the turn, and show a
# friendly note rather than the raw litellm/aiohttp exception chain. Markers identify that
# connection/stall class loosely (by exception type + message) so the agent needn't import
# litellm's exception types; anything else keeps its own short text (e.g. "paused", which the web
# keys on for its paused state).
_STREAM_CONNECTION_MARKERS = (
    "timeout",
    "timed out",
    "socket",
    "apiconnection",
    "connection",
    "midstreamfallback",
    "read error",
    "econnreset",
)
_STREAM_STALLED_MESSAGE = (
    "The model stopped responding before the answer was finished — it may have been busy loading "
    "another model. Please try again."
)
_STREAM_INTERRUPTED_MESSAGE = "The answer was interrupted before it finished. Please try again."


def _stream_failure_messages(exc: Exception) -> tuple[str, str]:
    """Return ``(banner_detail, retained_note)`` for a mid-stream failure (#453).

    For the connection/stall class both are the friendly "model stopped responding" message, so
    the raw exception text never reaches the UI. For any other error the banner passes the
    exception's own (short) text through — so signals the web relies on, like "paused", survive —
    while a *retained* partial turn still gets a generic interrupted note rather than raw text.
    """
    blob = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in blob for marker in _STREAM_CONNECTION_MARKERS):
        return _STREAM_STALLED_MESSAGE, _STREAM_STALLED_MESSAGE
    return str(exc), _STREAM_INTERRUPTED_MESSAGE


def _tool_detail(arguments: dict[str, Any]) -> str | None:
    """Compact JSON of a tool call's arguments for the step's expandable detail (or None)."""
    if not arguments:
        return None
    try:
        rendered = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return rendered[:_TOOL_DETAIL_CAP]


#: Resolves a tool name to ``(module_name, annotation)`` when the tool declares
#: ``writes_document``, else ``None`` (#541, ADR-0100). Backed by the module registry's
#: manifests; injected so the agent loop needn't know the registry exists.
DocumentToolLookup = Callable[[str], Awaitable[tuple[str, WritesDocument] | None]]


def _document_payload(
    module: str, spec: WritesDocument, arguments: dict[str, Any]
) -> dict[str, Any] | None:
    """What an annotated call is writing, for the shell's document pane (#541, ADR-0101).

    ``None`` when the call carries no usable body — the annotation promises the argument
    exists (the manifest validates that), not that the model filled it with a string. A pane
    with nothing to show is worse than no pane, and this is best-effort either way.
    """
    content = arguments.get(spec.content_arg)
    if not isinstance(content, str) or not content:
        return None

    def named(arg: str | None) -> str | None:
        value = arguments.get(arg) if arg else None
        return value if isinstance(value, str) and value else None

    return {
        "module": module,
        "content": content,
        "target": named(spec.target_arg),
        "title": named(spec.title_arg),
    }


class TurnUsage(BaseModel):
    """What a turn cost, summed across its steps (ADR-0105).

    A turn is one *or more* gateway calls — every tool round is another completion — so the
    interesting number is the total, not the last one. Both counts stay ``None`` until a
    call actually reports usage: a provider that returns none must read as "unknown", not
    as "free". Reporting 0 for an unmetered turn would quietly understate every bill.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    steps: int = 0

    def add(self, result: ChatResult) -> None:
        """Fold one completion's usage in."""
        self.steps += 1
        if result.prompt_tokens is not None:
            self.prompt_tokens = (self.prompt_tokens or 0) + result.prompt_tokens
        if result.completion_tokens is not None:
            self.completion_tokens = (self.completion_tokens or 0) + result.completion_tokens


class AgentTurn(BaseModel):
    """The result of one agent turn."""

    content: str
    tools_used: list[str] = Field(default_factory=list)
    # Why the turn ended: "completed" (the model answered) · "max_steps" (the loop bound) ·
    # "repeat_call" / "tool_errors" (loop-hygiene early stops, #524) · "error" (a mid-stream
    # failure, streaming only). The web can key stop-reason copy off it.
    stopped: str
    # Module entities the turn referenced, lifted from tool outputs (ADR-0019).
    entity_refs: list[EntityRef] = Field(default_factory=list)
    # The turn's process — thinking + tool steps — persisted so the activity timeline
    # survives a reopen, not only the live stream (ADR-0041).
    activity: MessageActivity = Field(default_factory=MessageActivity)
    # What the turn cost (ADR-0105). The automations ledger records it per run; an ordinary
    # turn ignores it. Only the non-streaming path fills it — the streamed one is not
    # token-metered by the providers today — and automations run non-streaming.
    usage: TurnUsage = Field(default_factory=TurnUsage)


def _entity_refs_for_model(refs: list[EntityRef], *, tenant_id: str | None = None) -> str:
    """A compact, model-facing listing of a tool result's entity refs and their ids (#449).

    Modules return entity refs on an envelope for the UI to render as chips, but their text
    typically names entities *without* an id (``calendar_list_events`` prints
    ``- {title} ({when})``) — so a model that lists, then wants to act on one ("edit that event"),
    has no id to pass to the edit/delete tool. Each ref carries ``ref_id`` (the id the owning
    module's tools accept), so we append them to the text the model sees. This fixes the class of
    bug for **every** module with refs (ADR-0079), not via a per-module workaround. The block is
    part of the tool *result* — model-only context, never rendered in chat — so unlike an inline
    marker in displayed text it needs no display-stripping.

    A large ref list (RRULE-expanded calendar events, a wide search) roughly doubles its
    context cost once each id is echoed here too, so the block itself is capped at
    :data:`~epicurus_core.LIST_CAP` (#468) — independent of ``entity_refs`` on the envelope,
    which stays uncapped and unchanged for the UI's chips. ``tenant_id`` is only for the
    truncation log line; it plays no part in which refs are shown.
    """
    capped = refs[:LIST_CAP]
    lines = [f"- {ref.title} — id: {ref.ref_id} ({ref.module} {ref.kind})" for ref in capped]
    if len(refs) > LIST_CAP:
        log.warning(
            "entity-ref id block truncated for the model",
            tenant_id=tenant_id,
            total=len(refs),
            shown=LIST_CAP,
        )
        intro = (
            f"\n\nReferenced items — showing {LIST_CAP} of {len(refs)} (pass an item's id to"
            " a tool that needs one — e.g. to open, edit, or delete it; narrow the query/range"
            " or ask for more to see the rest):\n"
        )
    else:
        intro = (
            "\n\nReferenced items (pass an item's id to a tool that needs one — e.g. to open, "
            "edit, or delete it):\n"
        )
    return intro + "\n".join(lines)


def _extract_entities(output: str, *, tenant_id: str | None = None) -> tuple[str, list[EntityRef]]:
    """Split a tool's output into (text for the model, entity references).

    A tool may return a JSON :class:`ToolEnvelope` (``{text, entity_refs}``); if so the text is
    fed back to the model — with a compact listing of the refs' ids appended so the model can act
    on them (:func:`_entity_refs_for_model`, #449) — and the refs are lifted onto the turn for the
    UI's chips. Anything else — plain text, an ``error:`` string, or unrelated JSON — is returned
    unchanged with no refs, so existing tools keep working. ``tenant_id`` only threads through to
    the id block's truncation log (#468).
    """
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return output, []
    if not (
        isinstance(data, dict)
        and isinstance(data.get("text"), str)
        and isinstance(data.get("entity_refs"), list)
    ):
        return output, []
    try:
        envelope = ToolEnvelope.model_validate(data)
    except ValidationError:
        return output, []
    if not envelope.entity_refs:
        return envelope.text, envelope.entity_refs
    block = _entity_refs_for_model(envelope.entity_refs, tenant_id=tenant_id)
    return envelope.text + block, envelope.entity_refs


def _parse_draft(output: str) -> DraftReview | None:
    """A tool's output as a :class:`DraftReview`, or ``None`` if it isn't one (ADR-0085).

    A *compose* tool (mail's ``mail_send`` / ``mail_reply``) returns a JSON ``DraftReview``
    (``{kind, module, draft, …}``) to request the outbound-approval pause; the loop recognizes it
    the way :func:`_extract_entities` recognizes a ``ToolEnvelope`` and suspends instead of feeding
    the result back to the model. Anything else — plain text, an ``error:`` hint (a compose that
    failed, e.g. a missing scope), a ``ToolEnvelope`` — returns ``None``, so every other tool is
    unaffected.
    """
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return None
    if not (
        isinstance(data, dict)
        and isinstance(data.get("kind"), str)
        and isinstance(data.get("module"), str)
        and isinstance(data.get("draft"), dict)
    ):
        return None
    try:
        return DraftReview.model_validate(data)
    except ValidationError:
        return None


@dataclass
class _Pending:
    """A tool call that pauses the turn — an ``ask_user`` question or a compose-tool draft.

    At most one per step (the loop gates on ``pending is None``): the first suspend-requesting
    call in a step wins and the turn suspends after its siblings run, so the conversation stays
    valid (every tool call gets a result or is the single deferred one). ``draft`` is set for a
    ``kind == "draft"`` pending; ``question`` for ``"ask_user"``.
    """

    call_id: str
    kind: str  # "ask_user" | "draft"
    tool: str
    question: str = ""
    draft: DraftReview | None = None


class _RefCollector:
    """Accumulates entity references across a turn's tool calls, de-duplicated."""

    def __init__(self) -> None:
        self.refs: list[EntityRef] = []
        self._seen: set[tuple[str, str, str]] = set()

    def add(self, refs: list[EntityRef]) -> None:
        for ref in refs:
            key = (ref.module, ref.kind, ref.ref_id)
            if key not in self._seen:
                self._seen.add(key)
                self.refs.append(ref)


class AttachmentExpander(Protocol):
    """Resolves a turn's attachments into text + images the agent injects (ADR-0019, #633)."""

    async def expand(
        self, attachments: list[Attachment], *, tenant: str
    ) -> ExpandedAttachments: ...


class AgentEvent(BaseModel):
    """One event of a streaming agent turn (the SSE protocol's payload).

    ``delta`` carries a content token; ``tool`` reports a tool call's progress
    (``running`` → ``ok``/``error``); ``done`` carries the final turn; ``error``
    ends a failed stream. A ``readiness`` event may *lead* the stream (warming
    progress; emitted by the route, not the loop) — see ADR-0027. ``awaiting_input``
    ends the stream when the model calls ``ask_user``: it carries the ``question`` and a
    ``run_id`` the client posts the answer to, to resume the turn (ADR-0053). The same
    ``awaiting_input`` frame carries a draft-review pause (ADR-0085, #563) when ``awaiting_kind``
    is ``"draft_review"``: it then carries the composed ``draft`` to render in the split-pane, and
    the ``run_id`` is confirmed/declined via ``POST /runs/{run_id}/draft``. Reusing the existing
    event type (rather than a new one) keeps a stale, service-worker-cached PWA parsing the stream
    — every new field is additive (ADR-0055).
    """

    type: str  # "delta" | "tool" | "done" | "error" | "readiness" | "awaiting_input"
    text: str | None = None
    tool: str | None = None
    status: str | None = None
    turn: AgentTurn | None = None
    detail: str | None = None
    readiness: Readiness | None = None
    # awaiting_input (ask_user, ADR-0053): the question to put to the user + the run to resume.
    run_id: str | None = None
    question: str | None = None
    # awaiting_input, draft-review flavour (ADR-0085): ``"draft_review"`` + the composed draft the
    # shell renders in the split-pane for Confirm/Decline. Absent for an ``ask_user`` pause.
    awaiting_kind: str | None = None
    draft: dict[str, Any] | None = None
    # ``tool`` events only, and only for a tool the module annotated ``writes_document``
    # (#541, ADR-0100/0101): what the call is writing — ``{module, content, target, title}`` —
    # so the shell can open the document pane beside the chat. Rides both the ``running`` and
    # the terminal frame (the pane opens on one and unlocks on the other). Deliberately kept
    # off the persisted ``ToolStep``: a document body is unbounded, and ADR-0041's activity
    # caps are not the place to store one.
    document: dict[str, Any] | None = None


def _parse_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Pull ``(name, arguments, id)`` out of an OpenAI-style tool call."""
    function = call.get("function") or {}
    name = function.get("name") or ""
    raw = function.get("arguments")
    if isinstance(raw, dict):
        arguments = raw
    elif isinstance(raw, str):
        try:
            arguments = json.loads(raw or "{}")
        except json.JSONDecodeError:
            arguments = {}
    else:
        arguments = {}
    return name, arguments, call.get("id") or ""


# Loop hygiene (#524). The thin loop (ADR-0001) continues on the blunt rule "the model made a
# tool call", up to max_steps. Two shapes burn the whole budget and end in a silent stop: the
# model re-issuing the *same* call, and a run of consecutive tool errors (retrying a broken call
# to exhaustion). The guard below wraps the loop with outcome-aware *stopping* — not planning — so
# ADR-0001's thinness holds. Both nudges are one-shot per turn, like _ANSWER_NUDGE.
_REPEAT_NUDGE = (
    "You just made that exact tool call with the same arguments — its result is already above. "
    "Use that result to answer, or try something different; do not repeat the same call."
)
_REPEAT_TOOL_NOTICE = (
    "This exact call was just made with the same arguments; its earlier result above still "
    "stands. It was not run again — use that result or give your final answer."
)
# Stop the turn after this many consecutive tool errors rather than exhausting max_steps.
_MAX_CONSECUTIVE_TOOL_ERRORS = 3

# ``stopped`` reasons beyond "completed" | "max_steps", surfaced on AgentTurn.stopped (#524).
_STOPPED_REPEAT_CALL = "repeat_call"
_STOPPED_TOOL_ERRORS = "tool_errors"


def _canonical_calls(tool_calls: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    """A stable signature for a step's tool calls — ``(name, canonical-args)`` per call, order-free.

    Canonicalizes each call to ``(name, json(args, sort_keys))`` and sorts the set, so the same
    calls in a different order compare equal but *different arguments* (paging, per-item work) do
    not. This is what lets repeat detection fire on an identical re-issue yet leave a legitimate
    distinct-args repeat untouched (#524).
    """
    signature: list[tuple[str, str]] = []
    for call in tool_calls:
        name, arguments, _ = _parse_tool_call(call)
        try:
            canon = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            canon = repr(arguments)
        signature.append((name, canon))
    return tuple(sorted(signature))


class _LoopGuard:
    """Outcome-aware stop detection wrapped around the thin loop (ADR-0001 stays thin, #524).

    Detects two turn-fatal shapes without entangling the loop, so both ``run`` (``_loop``) and
    ``run_stream`` apply the same rule by asking the same object:

    * **identical repeat** — the model re-issues the exact same call(s). The first repeat earns a
      one-shot nudge (``"nudge"``); a further repeat stops the turn (``"stop"``), mirroring the
      one-shot ``_ANSWER_NUDGE`` for a blank answer.
    * **error streak** — :data:`_MAX_CONSECUTIVE_TOOL_ERRORS` consecutive tool errors (the model
      retrying a broken call to exhaustion) stops the turn early, so the user gets "here's what
      failed" instead of a silent stall.
    """

    def __init__(self) -> None:
        self._prev_signature: tuple[tuple[str, str], ...] | None = None
        self.repeat_nudged = False
        self.error_streak = 0

    def repeat_verdict(self, tool_calls: list[dict[str, Any]]) -> str:
        """Classify this step's calls vs. the previous step: ``"new"`` | ``"nudge"`` | ``"stop"``.

        Updates the remembered signature every call, so a distinct step in between resets the
        comparison — only an *immediately* repeated call is caught.
        """
        signature = _canonical_calls(tool_calls)
        is_repeat = self._prev_signature is not None and signature == self._prev_signature
        self._prev_signature = signature
        if not is_repeat:
            return "new"
        if not self.repeat_nudged:
            self.repeat_nudged = True
            return "nudge"
        return "stop"

    def note_results(self, errored: list[bool]) -> bool:
        """Fold this step's tool outcomes into the running streak; ``True`` once it hits the bound.

        Any success resets the streak — the guard fires only on *consecutive* errors, so a turn
        that errors once and then recovers is left untouched (no behavior change for healthy turns).
        """
        for is_error in errored:
            self.error_streak = self.error_streak + 1 if is_error else 0
        return self.error_streak >= _MAX_CONSECUTIVE_TOOL_ERRORS


class Agent:
    """Drives the LLM gateway plus module tools to answer a turn."""

    def __init__(
        self,
        *,
        gateway: LlmGateway,
        mcp: McpHost,
        memory: Memory | None = None,
        max_steps: int = 4,
        default_tenant: str = "local",
        attachments: AttachmentExpander | None = None,
        extractor: FactExtractor | None = None,
        queue: ExtractionQueue | None = None,
        defer_extraction: bool = True,
        recall_timeout_s: float = _DEFAULT_RECALL_TIMEOUT_S,
        prefs: LlmPrefsStore | None = None,
        suspended: SuspendedRunStore | None = None,
        pending_drafts: PendingDraftStore | None = None,
        instructions: AgentInstructionsStore | None = None,
        profile: StandingProfileStore | None = None,
        documents: DocumentToolLookup | None = None,
    ) -> None:
        self._gateway = gateway
        self._mcp = mcp
        # Resolves a tool name to its module + ``writes_document`` annotation (#541, ADR-0100),
        # so a document-writing call can tell the shell what it is writing. The annotation lives
        # only in the module manifest — MCP's ``list_tools`` drops it — so this is the registry's
        # read-only view, injected rather than imported to keep the loop free of the registry.
        # None disables the document pane; the turn is otherwise identical.
        self._documents = documents
        self._memory = memory
        self._max_steps = max_steps
        self._default_tenant = default_tenant
        self._attachments = attachments
        # Fact extraction (ADR-0045/0051): after a turn, distil durable user facts. By default
        # the exchange is *deferred* to ``queue`` and a nightly runner distils it off-hours, so
        # extraction never competes with the next turn for the GPU. ``defer_extraction=False``
        # restores the immediate path — fire ``extractor`` as a background task. Tasks are
        # tracked so they aren't GC'd mid-flight.
        self._extractor = extractor
        self._queue = queue
        self._defer_extraction = defer_extraction
        self._recall_timeout_s = recall_timeout_s
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._prefs = prefs
        # Suspend store for ask_user pause/resume (ADR-0053). None disables pausing (the loop
        # then degrades: ask_user gets an instruction to proceed rather than suspending).
        self._suspended = suspended
        # Pending-draft store for draft-first send Confirm/Decline (ADR-0085). None disables the
        # pause (the loop then degrades: a compose tool is told it cannot present a draft to send).
        self._pending_drafts = pending_drafts
        # Editable base system prompt (#497, ADR-0083). None disables it (the agent runs with no
        # base prompt, as it did before this store existed); resolved per turn in ``_assemble``.
        self._instructions = instructions
        # Standing user profile (#527, ADR-0094): a compact, nightly-synthesized picture of the
        # user, injected STATICALLY (no turn-time embed) in ``_assemble``. None disables it (no
        # profile → exactly today's behavior); best-effort, so a read failure never breaks a turn.
        self._profile = profile

    async def _effective_max_steps(self, tenant_id: str | None) -> int:
        """The active agent loop bound: the stored pref if set, else the env default.

        Resolved per turn so the operator's UI choice takes effect without a restart
        (the agent is constructed once). The route clamps the stored value's range.
        """
        if self._prefs is not None:
            stored = await self._prefs.get_agent_max_steps(tenant_id or self._default_tenant)
            if stored is not None:
                return stored
        return self._max_steps

    async def run(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        allow: frozenset[SideEffect] | None = None,
        automation_id: str | None = None,
    ) -> AgentTurn:
        """Run one turn to completion (or until ``max_steps`` tool rounds).

        With ``session_id`` and memory configured, the turn is grounded in the
        session's prior messages plus semantically recalled context, and both the
        new user input and the answer are persisted for future turns.

        ``allow`` restricts the turn's tool surface to tools of those side-effect classes
        (ADR-0105) — how an automation's autonomy level is *enforced* rather than merely
        requested. ``None`` (an ordinary turn) offers everything enabled. ``automation_id``
        attributes the turn's gateway usage to the automation that caused it, alongside the
        tenant — the dual attribution the SaaS overlay meters on.
        """
        tenant = tenant_id or self._default_tenant
        messages, images = await self._expand_attachments(messages, tenant=tenant)
        convo = await self._assemble(messages, tenant=tenant, session_id=session_id)
        blocked = bool(images) and not await self._gateway.supports_vision(model, tenant_id)
        if blocked:
            turn = _vision_unsupported_turn()
        else:
            if images:
                convo = _attach_images(convo, images)
            turn = await self._loop(
                convo,
                model=model,
                tenant_id=tenant_id,
                allow=allow,
                automation_id=automation_id,
            )
        await self._persist_answer(turn, tenant=tenant, session_id=session_id)
        if not blocked:
            self._schedule_extraction(tenant=tenant, messages=messages, answer=turn.content)
        return turn

    async def run_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        persist_input: bool = True,
        resume_convo: list[ChatMessage] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Stream one turn as it happens: deltas, tool progress, then ``done``.

        Memory semantics match :meth:`run`. The turn's content is exactly the text
        the caller watched stream by. A failure mid-stream ends with an ``error``
        event rather than an exception (the HTTP response has already started).

        ``persist_input=False`` with empty ``messages`` re-answers the stored tail without
        adding a new user message (regenerate / edit, #302).

        ``resume_convo`` continues a turn that ``ask_user`` paused (ADR-0053): the caller
        passes the rehydrated conversation (history + the answer as the pending tool result),
        so assembly/persistence of new input is skipped and the loop just continues.
        """
        tenant = tenant_id or self._default_tenant
        max_steps = await self._effective_max_steps(tenant)
        images: list[ImagePart] = []
        if resume_convo is not None:
            convo = resume_convo
        else:
            messages, images = await self._expand_attachments(messages, tenant=tenant)
            convo = await self._assemble(
                messages, tenant=tenant, session_id=session_id, persist_input=persist_input
            )
        if images and not await self._gateway.supports_vision(model, tenant_id):
            # Gate before any provider call (#633): a non-vision model would either mangle
            # the image or have the provider itself reject it — explain the limitation as a
            # normal turn instead (same shape as the mid-stream failure path below).
            turn = _vision_unsupported_turn()
            yield AgentEvent(type="delta", text=turn.content)
            await asyncio.shield(self._persist_answer(turn, tenant=tenant, session_id=session_id))
            yield AgentEvent(type="done", turn=turn)
            return
        if images:
            convo = _attach_images(convo, images)
        parts: list[str] = []
        timeline: list[ActivityItem] = []
        tools_used: list[str] = []
        refs = _RefCollector()
        stopped = "completed"
        reasoned = False  # the model emitted <think> reasoning at least once this turn
        nudged = False  # we already nudged a blank step to commit to an answer (do it once)
        guard = _LoopGuard()  # outcome-aware stop detection (#524), same rule as run()'s _loop
        need_final = False  # an early/exhausted stop wants one final tool-less answer streamed
        try:
            specs, route = await self._mcp.discover()
            # Offer tools only to a model that can use them; otherwise the runtime errors and
            # the turn fails. A tool-less model just answers in text (the UI flags it).
            can_use_tools = bool(specs) and await self._gateway.supports_tools(model, tenant_id)
            offer = specs if can_use_tools else None
            for _ in range(max_steps):
                result: ChatResult | None = None
                answer_before = len(parts)
                async for event in self._gateway.stream_chat(
                    convo, model=model, tools=offer, tenant_id=tenant_id
                ):
                    if event.delta:
                        parts.append(event.delta)
                        yield AgentEvent(type="delta", text=event.delta)
                    if event.reasoning:
                        reasoned = True
                        append_thinking(timeline, event.reasoning)
                        yield AgentEvent(type="thinking", text=event.reasoning)
                    if event.result is not None:
                        result = event.result
                if result is None or not result.tool_calls:
                    # The model answered (text streamed) or it produced nothing. If nothing — a
                    # reasoning model that thought but never answered — nudge it once and retry,
                    # rather than ending the turn empty (which renders as the silent "stop").
                    if len(parts) > answer_before or nudged:
                        break
                    nudged = True
                    convo.append(
                        ChatMessage(role="assistant", content=result.content if result else "")
                    )
                    convo.append(ChatMessage(role="user", content=_ANSWER_NUDGE))
                    continue
                convo.append(
                    ChatMessage(
                        role="assistant", content=result.content, tool_calls=result.tool_calls
                    )
                )
                verdict = guard.repeat_verdict(result.tool_calls)
                if verdict != "new":
                    # The model re-issued the exact same call(s). Don't run them again (a repeated
                    # write would double-apply) — stub each result so the conversation stays valid,
                    # then nudge once (like _ANSWER_NUDGE) or stop the turn on a further repeat. A
                    # repeated ask_user/draft can't reach here — the first one suspends and returns.
                    for call in result.tool_calls:
                        _name, _args, call_id = _parse_tool_call(call)
                        convo.append(
                            ChatMessage(
                                role="tool",
                                tool_call_id=call_id,
                                name=_name,
                                content=_REPEAT_TOOL_NOTICE,
                            )
                        )
                    if verdict == "nudge":
                        convo.append(ChatMessage(role="user", content=_REPEAT_NUDGE))
                        continue
                    stopped = _STOPPED_REPEAT_CALL
                    need_final = True
                    break
                errored: list[bool] = []  # per-tool error flags this step, for the streak guard
                pending: _Pending | None = None  # the one turn-pausing call this step, if any
                for call in result.tool_calls:
                    name, arguments, call_id = _parse_tool_call(call)
                    if name == ASK_USER_TOOL:
                        # Don't execute — ask_user suspends the turn (ADR-0053). Defer the
                        # suspend until this step's other calls have run, so every tool_call
                        # gets a result and the conversation stays valid on resume.
                        if pending is None:
                            question = str(arguments.get("question") or "").strip()
                            pending = _Pending(
                                call_id=call_id, kind="ask_user", tool=name, question=question
                            )
                            tools_used.append(name)
                            append_tool(timeline, name, "ok", _tool_detail(arguments))
                            yield AgentEvent(
                                type="tool", tool=name, status="ok", detail=question or None
                            )
                        else:  # a second pause in one step — stub it so the convo stays valid
                            convo.append(
                                ChatMessage(
                                    role="tool",
                                    tool_call_id=call_id,
                                    name=name,
                                    content="(answered together with the pause above)",
                                )
                            )
                        continue
                    tools_used.append(name)
                    detail = _tool_detail(arguments)
                    document = await self._document_written_by(name, arguments)
                    yield AgentEvent(
                        type="tool", tool=name, status="running", detail=detail, document=document
                    )
                    output, is_error = await self._invoke(name, arguments, route, tenant=tenant)
                    text, found = _extract_entities(output, tenant_id=tenant)
                    refs.add(found)
                    draft = None if is_error else _parse_draft(output)
                    if draft is not None:
                        # The tool composed an outbound draft for review (ADR-0085): suspend the
                        # turn instead of feeding the envelope back to the model. Its tool result
                        # is filled on Confirm (sent) / Decline (not sent). Only the first pause in
                        # a step suspends; a later one is stubbed so the conversation stays valid.
                        if pending is None:
                            pending = _Pending(
                                call_id=call_id, kind="draft", tool=name, draft=draft
                            )
                            append_tool(timeline, name, "ok", draft.summary or None)
                            yield AgentEvent(
                                type="tool", tool=name, status="ok", detail=draft.summary or None
                            )
                        else:
                            convo.append(
                                ChatMessage(
                                    role="tool",
                                    tool_call_id=call_id,
                                    name=name,
                                    content="(not shown — another review is pending above)",
                                )
                            )
                        continue
                    status = "error" if is_error else "ok"
                    errored.append(is_error)
                    yield AgentEvent(
                        type="tool", tool=name, status=status, detail=detail, document=document
                    )
                    # `document` is deliberately absent here: the timeline is persisted per
                    # message (ADR-0041) and a document body has no place in those caps.
                    append_tool(timeline, name, status, detail)
                    convo.append(
                        ChatMessage(role="tool", tool_call_id=call_id, name=name, content=text)
                    )
                if pending is not None:
                    run_id = await self._suspend_pending(
                        pending, convo=convo, model=model, tenant=tenant, session_id=session_id
                    )
                    if run_id is not None:
                        # Pause the turn (no `done`): ask_user posts an answer to /resume; a draft
                        # is confirmed/declined via /runs/{run_id}/draft (ADR-0053/ADR-0085).
                        if pending.kind == "draft" and pending.draft is not None:
                            yield AgentEvent(
                                type="awaiting_input",
                                run_id=run_id,
                                awaiting_kind="draft_review",
                                draft=pending.draft.draft,
                            )
                        else:
                            yield AgentEvent(
                                type="awaiting_input", run_id=run_id, question=pending.question
                            )
                        return
                    # No suspend store wired — degrade: give the model a result and keep going.
                    convo.append(
                        ChatMessage(
                            role="tool",
                            tool_call_id=pending.call_id,
                            name=pending.tool,
                            content=(
                                "error: cannot present a draft to send right now; tell the user you"
                                " could not send it and to try again."
                                if pending.kind == "draft"
                                else "error: cannot pause for input; use your best assumption"
                            ),
                        )
                    )
                if guard.note_results(errored):
                    # A streak of consecutive tool errors — stop early and answer with what failed,
                    # rather than letting the model retry a broken call until max_steps.
                    stopped = _STOPPED_TOOL_ERRORS
                    need_final = True
                    break
            else:  # steps exhausted — one final tool-less answer streamed below
                stopped = "max_steps"
                need_final = True
            if need_final:
                # A non-answer exit (max_steps, or a hygiene early stop) streams one final tool-less
                # answer, so the turn ends with a real reply — "here's what I found / what failed" —
                # never a silent stop. One call, not the unbounded retrying the guard just cut off.
                async for event in self._gateway.stream_chat(
                    convo, model=model, tenant_id=tenant_id
                ):
                    if event.delta:
                        parts.append(event.delta)
                        yield AgentEvent(type="delta", text=event.delta)
                    if event.reasoning:
                        reasoned = True
                        append_thinking(timeline, event.reasoning)
                        yield AgentEvent(type="thinking", text=event.reasoning)
        except Exception as exc:  # the response already started — degrade gracefully (#453)
            log.warning("streaming turn failed", error=str(exc))
            banner, note = _stream_failure_messages(exc)
            partial = "".join(parts)
            if not (partial.strip() or timeline):
                # Nothing was produced before the failure — no partial worth keeping. Surface a
                # friendly banner (or the error's own short text, e.g. "paused"), then stop.
                yield AgentEvent(type="error", detail=banner)
                return
            # Keep the partial answer + activity rather than discarding the turn: stream the note
            # (the in-chat "friendly error"), persist the partial so a reopen still shows it, and
            # finish the stream cleanly. The raw exception stays in the log only. `stopped=error`
            # marks the turn incomplete without leaking internals (it is not surfaced to the user).
            lead = "\n\n" if partial.strip() else ""
            yield AgentEvent(type="delta", text=f"{lead}{note}")
            turn = AgentTurn(
                content=f"{partial}{lead}{note}",
                tools_used=tools_used,
                stopped="error",
                entity_refs=refs.refs,
                activity=activity_from_timeline(timeline, thinking_cap=_THINKING_CAP),
            )
            # Shield the write like the normal path (#376): a shutdown cancellation now must still
            # flush the partial we chose to keep. Extraction is skipped — an interrupted turn is
            # not something to learn durable facts from.
            await asyncio.shield(self._persist_answer(turn, tenant=tenant, session_id=session_id))
            yield AgentEvent(type="done", turn=turn)
            return
        content = "".join(parts)
        if not content.strip():
            # No tool calls and no answer text, even after a nudge — never persist or stream an
            # empty turn (it renders as a silent stop). Surface a clear fallback, and log why so
            # the operator can see it was e.g. a reasoning model that thought but never answered.
            log.warning(
                "turn produced no answer; using fallback",
                model=model,
                stopped=stopped,
                reasoned=reasoned,
                nudged=nudged,
            )
            content = _EMPTY_ANSWER_FALLBACK
            yield AgentEvent(type="delta", text=content)
        turn = AgentTurn(
            content=content,
            tools_used=tools_used,
            stopped=stopped,
            entity_refs=refs.refs,
            activity=activity_from_timeline(timeline, thinking_cap=_THINKING_CAP),
        )
        # Shield only the answer write: the model already produced the reply, so a cancellation
        # arriving now (server shutdown — the turn runs in a detached task, see live_runs.py)
        # must still flush it. The model call above stays promptly cancellable; losing the
        # finished answer here would be the very bug this decoupling fixes (#376).
        await asyncio.shield(self._persist_answer(turn, tenant=tenant, session_id=session_id))
        self._schedule_extraction(tenant=tenant, messages=messages, answer=turn.content)
        yield AgentEvent(type="done", turn=turn)

    def _schedule_extraction(
        self, *, tenant: str, messages: list[ChatMessage], answer: str
    ) -> None:
        """Persist this exchange for fact extraction, off the response path (ADR-0045/0051).

        Default (deferred): enqueue the exchange for the nightly runner — a quick, durable
        insert, so extraction won't compete with the next turn for the GPU. Immediate mode
        (``defer_extraction=False``): fire the extractor now as a background task (the legacy
        ADR-0045 path). Either way it is fire-and-forget so it never delays the reply, and the
        task is tracked until it finishes so it isn't garbage-collected mid-flight. Skips when
        there is nothing to learn from — no user text, no answer, or only the empty-answer
        fallback (a canned non-answer) — or when no sink is configured.
        """
        if not answer or answer == _EMPTY_ANSWER_FALLBACK:
            return
        user_text = next(
            (
                text
                for m in reversed(messages)
                if m.role == "user" and (text := _text_only(m.content))
            ),
            None,
        )
        if not user_text:
            return
        coro: Coroutine[Any, Any, object]
        if self._defer_extraction and self._queue is not None:
            coro = self._queue.enqueue(tenant=tenant, user_text=user_text, assistant_text=answer)
        elif self._extractor is not None:
            coro = self._extractor.extract(
                tenant=tenant, user_text=user_text, assistant_text=answer
            )
        else:
            return
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _persist_answer(
        self, turn: AgentTurn, *, tenant: str, session_id: str | None
    ) -> None:
        if self._memory is None or not session_id:
            return
        try:
            await self._memory.remember(
                tenant=tenant,
                session_id=session_id,
                role="assistant",
                content=turn.content,
                entity_refs=[ref.model_dump() for ref in turn.entity_refs],
                # Persist the process only when there is one — keep plain turns blob-free.
                activity=None if turn.activity.is_empty() else turn.activity.model_dump(),
            )
        except Exception as exc:  # a failed write must not lose the answer
            log.warning("memory write failed", error=str(exc))

    async def _suspend(
        self,
        *,
        convo: list[ChatMessage],
        model: str | None,
        tenant: str,
        session_id: str | None,
        pending_call_id: str,
        question: str,
    ) -> str | None:
        """Persist the in-progress run so an answer can resume it (ADR-0053).

        Returns the run id, or ``None`` when no suspend store is wired — the caller then
        degrades gracefully (instructs the model to proceed) instead of pausing.
        """
        if self._suspended is None:
            return None
        try:
            return await self._suspended.save(
                tenant=tenant,
                session_id=session_id,
                model=model,
                pending_call_id=pending_call_id,
                question=question,
                conversation=[m.model_dump(exclude_none=True) for m in convo],
            )
        except Exception as exc:  # a failed persist must not crash the turn
            log.warning("suspend persist failed; proceeding without pause", error=str(exc))
            return None

    async def _suspend_pending(
        self,
        pending: _Pending,
        *,
        convo: list[ChatMessage],
        model: str | None,
        tenant: str,
        session_id: str | None,
    ) -> str | None:
        """Persist a turn-pausing call to the store for its kind; return the run id (or ``None``).

        Dispatches an ``ask_user`` pause to the suspended-run store (ADR-0053) and a compose-tool
        draft to the pending-draft store (ADR-0085); ``None`` (no store wired) degrades to a
        proceed/notice tool result at the call site.
        """
        if pending.kind == "draft":
            assert pending.draft is not None
            return await self._suspend_draft(
                convo=convo,
                model=model,
                tenant=tenant,
                session_id=session_id,
                pending_call_id=pending.call_id,
                tool=pending.tool,
                draft=pending.draft,
            )
        return await self._suspend(
            convo=convo,
            model=model,
            tenant=tenant,
            session_id=session_id,
            pending_call_id=pending.call_id,
            question=pending.question,
        )

    async def _suspend_draft(
        self,
        *,
        convo: list[ChatMessage],
        model: str | None,
        tenant: str,
        session_id: str | None,
        pending_call_id: str,
        tool: str,
        draft: DraftReview,
    ) -> str | None:
        """Persist the run + composed draft so a Confirm/Decline can resume it (ADR-0085).

        Returns the run id, or ``None`` when no pending-draft store is wired — the caller then
        degrades (tells the model it could not present the draft) instead of pausing.
        """
        if self._pending_drafts is None:
            return None
        try:
            return await self._pending_drafts.save(
                tenant=tenant,
                session_id=session_id,
                model=model,
                pending_call_id=pending_call_id,
                tool=tool,
                module=draft.module,
                summary=draft.summary,
                draft=draft.draft,
                conversation=[m.model_dump(exclude_none=True) for m in convo],
            )
        except Exception as exc:  # a failed persist must not crash the turn
            log.warning("draft suspend persist failed; proceeding without pause", error=str(exc))
            return None

    async def _expand_attachments(
        self, messages: list[ChatMessage], *, tenant: str
    ) -> tuple[list[ChatMessage], list[ImagePart]]:
        """Resolve any attachments on the user's message into a leading system message.

        Best-effort (ADR-0019): an expander failure or empty result leaves the turn
        untouched. The attachments themselves stay on the user message (persisted +
        stripped before any provider call); only their resolved content is injected here.

        Image attachments are returned separately, never through the text preamble (#633):
        the caller checks the selected model's vision capability before deciding whether to
        attach them to the assembled convo — and never to what gets persisted, so a stored
        turn never balloons with base64 image data.
        """
        if self._attachments is None:
            return messages, []
        attached = [a for m in messages if m.role == "user" for a in (m.attachments or [])]
        if not attached:
            return messages, []
        try:
            resolved = await self._attachments.expand(attached, tenant=tenant)
        except Exception as exc:  # attachments are an enhancement, never a hard dependency
            log.warning("attachment expansion failed; proceeding without it", error=str(exc))
            return messages, []
        if resolved.text:
            preamble = ChatMessage(role="system", content=f"Attached context:\n{resolved.text}")
            messages = [preamble, *messages]
        return messages, resolved.images

    async def _recall_within_budget(self, *, tenant: str, query: str) -> list[str]:
        """Recall the facts relevant to ``query``, bounded by ``recall_timeout_s`` (ADR-0051).

        Recall embeds the query — the one memory step still on the response path. A cold or busy
        embedder must not delay the first token, so it is time-boxed; on timeout or any error the
        turn proceeds with no recalled facts (the same best-effort degrade as the rest of
        assemble), rather than blocking until our interaction with the model itself stalls.
        """
        if self._memory is None:
            return []
        start = time.monotonic()
        try:
            return await asyncio.wait_for(
                self._memory.recall(tenant=tenant, query=query), self._recall_timeout_s
            )
        except TimeoutError:
            # The embed didn't finish in the budget — usually a cold or busy embedder (on a
            # single GPU, an Ollama model swap). Degrade to no recall; name the budget so the
            # operator can see it timed out (vs. failed) and tune MEMORY_RECALL_TIMEOUT_S.
            log.warning(
                "recall skipped: embed timed out",
                timeout_s=self._recall_timeout_s,
                elapsed_s=round(time.monotonic() - start, 2),
            )
            return []
        except Exception as exc:  # embedder/Qdrant trouble — degrade to no recall, never block
            log.warning(
                "recall skipped: backend error",
                error=str(exc),
                error_type=type(exc).__name__,
                elapsed_s=round(time.monotonic() - start, 2),
            )
            return []

    async def _system_messages(self, tenant: str) -> list[ChatMessage]:
        """The base system prompt as a leading system message (#497, ADR-0083), or ``[]``.

        Resolved per turn from the tenant's editable instructions (else the shipped default), so
        an edit takes effect on the next turn with no restart — like the max-steps pref (#297).
        Placed **first** in the assembled conversation, ahead of recalled memory and attached
        context, so the compaction leading-prefix rule protects it. Applies to headless bridge
        turns too (ADR-0058): they carry no session/memory but still assemble through here. A read
        failure degrades to no prompt rather than breaking the turn.
        """
        if self._instructions is None:
            return []
        try:
            text = await self._instructions.get_instructions(tenant)
        except Exception as exc:  # a prompt read must never break the turn
            log.warning("instructions read failed; running with no base prompt", error=str(exc))
            return []
        return [ChatMessage(role="system", content=text)] if text.strip() else []

    async def _profile_messages(self, tenant: str) -> list[ChatMessage]:
        """The tenant's standing profile as a static system block (#527, ADR-0094), or ``[]``.

        The whole point is **no turn-time embedding**: recall pays an embed round-trip (and a
        single-GPU model-swap risk) every turn for the stable common case, so a nightly job distils
        that into this compact profile and it is injected verbatim — no vector search on the
        response path. Sits just after the base prompt and *before* recalled facts: the profile is
        the durable "who the user is"; recall stays for the long-tail specifics. Best-effort like
        the rest of memory — no profile, or a read failure, degrades to exactly today's behavior.
        Applies to headless bridge turns too (ADR-0058), which still assemble through here.
        """
        if self._profile is None:
            return []
        try:
            profile = await self._profile.latest(tenant=tenant)
        except Exception as exc:  # a profile read must never break the turn
            log.warning("standing-profile read failed; proceeding without it", error=str(exc))
            return []
        if profile is None or not profile.content.strip():
            return []
        return [
            ChatMessage(
                role="system",
                content=(
                    "Standing profile of the user (stable background; use it when relevant, "
                    f"don't recite it):\n{profile.content}"
                ),
            )
        ]

    async def _assemble(
        self,
        messages: list[ChatMessage],
        *,
        tenant: str,
        session_id: str | None,
        persist_input: bool = True,
    ) -> list[ChatMessage]:
        """Prepend recalled context + session history, then persist the new input.

        Memory is best-effort: any failure (DB, Qdrant, embeddings) degrades to a
        plain turn rather than breaking the chat.

        With ``persist_input=False`` and no new ``messages`` this re-answers the stored tail
        (regenerate / edit, #302): the recall query falls back to the last *stored* user turn,
        and nothing new is persisted — the user message is already in history.

        The base system prompt (#497) leads every path — the no-memory/headless early return, the
        assembled convo, and the degraded fallback — so it is always the first message; the static
        standing profile (#527) follows it on every path, ahead of any recalled facts.
        """
        system = await self._system_messages(tenant)
        profile = await self._profile_messages(tenant)
        if self._memory is None or not session_id:
            return [*system, *profile, *messages]
        try:
            convo: list[ChatMessage] = []
            history = await self._memory.history(tenant=tenant, session_id=session_id)
            # Recall off the new user input, else (re-answer) the last user turn in history.
            last_user = next(
                (
                    text
                    for m in reversed(messages)
                    if m.role == "user" and (text := _text_only(m.content))
                ),
                None,
            ) or next(
                (
                    text
                    for m in reversed(history)
                    if m.role == "user" and (text := _text_only(m.content))
                ),
                None,
            )
            if last_user:
                recalled = await self._recall_within_budget(tenant=tenant, query=last_user)
                if recalled:
                    joined = "\n".join(f"- {fact}" for fact in recalled)
                    convo.append(
                        ChatMessage(
                            role="system",
                            content=(
                                "What you remember about the user (use it when relevant; "
                                f"don't recite it):\n{joined}"
                            ),
                        )
                    )
            convo.extend(history)
            convo.extend(messages)
            if persist_input:
                for message in messages:
                    text = _text_only(message.content)
                    if message.role == "user" and text:
                        await self._memory.remember(
                            tenant=tenant,
                            session_id=session_id,
                            role="user",
                            content=text,
                            attachments=(
                                [a.model_dump() for a in message.attachments]
                                if message.attachments
                                else None
                            ),
                        )
            return [*system, *profile, *convo]
        except Exception as exc:  # memory is an enhancement, never a hard dependency
            log.warning("memory read failed; proceeding without it", error=str(exc))
            return [*system, *profile, *messages]

    async def _loop(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None,
        tenant_id: str | None,
        allow: frozenset[SideEffect] | None = None,
        automation_id: str | None = None,
    ) -> AgentTurn:
        """The tool-calling loop: ask, run tools, feed results back, until an answer."""
        # `allow` filters both what the model is told about and what `route` will dispatch,
        # so a withheld tool is unroutable, not merely unmentioned (ADR-0105).
        specs, route = await self._mcp.discover(allow=allow)
        call_tenant = tenant_id or self._default_tenant
        max_steps = await self._effective_max_steps(tenant_id)
        # Offer tools only to a tool-capable model (else the runtime errors); a tool-less model
        # just answers in text.
        offer = specs if specs and await self._gateway.supports_tools(model, tenant_id) else None
        convo = list(messages)
        tools_used: list[str] = []
        timeline: list[ActivityItem] = []
        refs = _RefCollector()
        usage = TurnUsage()  # summed across every step, for the automations ledger

        def activity() -> MessageActivity:
            return activity_from_timeline(timeline, thinking_cap=_THINKING_CAP)

        guard = _LoopGuard()  # outcome-aware stop detection (#524), wrapping the thin loop
        reasoned = False  # the model emitted <think> reasoning at least once this turn
        nudged = False  # we already nudged a blank step to commit to an answer (do it once)
        content = ""
        stopped = "completed"
        for _ in range(max_steps):
            result = await self._gateway.chat(
                convo,
                model=model,
                tools=offer,
                tenant_id=tenant_id,
                automation_id=automation_id,
            )
            usage.add(result)
            if result.reasoning:
                reasoned = True
                append_thinking(timeline, result.reasoning)
            if not result.tool_calls:
                # The model answered, or (a reasoning model) thought but produced nothing. If it
                # said nothing, nudge it once to commit to an answer rather than ending empty —
                # the same silent "stop" the streamed path guards against.
                if result.content.strip() or nudged:
                    content = result.content
                    break
                nudged = True
                convo.append(ChatMessage(role="assistant", content=result.content))
                convo.append(ChatMessage(role="user", content=_ANSWER_NUDGE))
                continue
            convo.append(
                ChatMessage(role="assistant", content=result.content, tool_calls=result.tool_calls)
            )
            verdict = guard.repeat_verdict(result.tool_calls)
            if verdict != "new":
                # The model re-issued the exact same call(s). Don't run them again — a repeated
                # write would double-apply — but stub each result so the conversation stays valid,
                # then nudge once (like _ANSWER_NUDGE) or stop the turn on a further repeat.
                for call in result.tool_calls:
                    _name, _args, call_id = _parse_tool_call(call)
                    convo.append(
                        ChatMessage(
                            role="tool",
                            tool_call_id=call_id,
                            name=_name,
                            content=_REPEAT_TOOL_NOTICE,
                        )
                    )
                if verdict == "nudge":
                    convo.append(ChatMessage(role="user", content=_REPEAT_NUDGE))
                    continue
                stopped = _STOPPED_REPEAT_CALL
                break
            errored: list[bool] = []
            for call in result.tool_calls:
                name, arguments, call_id = _parse_tool_call(call)
                tools_used.append(name)
                output, is_error = await self._invoke(name, arguments, route, tenant=call_tenant)
                text, found = _extract_entities(output, tenant_id=call_tenant)
                refs.add(found)
                status = "error" if is_error else "ok"
                if not is_error and _parse_draft(output) is not None:
                    # A compose tool returned an outbound draft for review (ADR-0085), but this
                    # non-streaming path (POST /chat, the messaging bridge) has no split-pane to
                    # Confirm/Decline in — the draft can't be sent here. Tell the model honestly
                    # rather than feeding back the raw envelope, which it would likely misreport as
                    # "sent". The transmit path is unreachable regardless, so nothing is ever sent;
                    # this only keeps the model from claiming otherwise.
                    text = (
                        "error: composed a draft, but it can't be sent from this channel — outbound"
                        " sends need review and Confirm in the chat UI, which isn't available here."
                        " Tell the user it was not sent."
                    )
                    status = "error"
                errored.append(status == "error")
                append_tool(timeline, name, status, _tool_detail(arguments))
                convo.append(
                    ChatMessage(role="tool", tool_call_id=call_id, name=name, content=text)
                )
            if guard.note_results(errored):
                # A streak of consecutive tool errors — stop early and answer with what failed,
                # rather than letting the model retry a broken call until max_steps.
                stopped = _STOPPED_TOOL_ERRORS
                break
        else:
            stopped = "max_steps"
        if stopped != "completed":
            # Any non-answer exit (max_steps, or a hygiene stop) gets one final tool-less answer,
            # so the turn ends with a real reply — "here's what I found / what failed" — never a
            # silent stop. One call, not the unbounded retrying the guard just cut off.
            final = await self._gateway.chat(convo, model=model, tenant_id=tenant_id)
            if final.reasoning:
                reasoned = True
                append_thinking(timeline, final.reasoning)
            content = final.content
        if not content.strip():
            log.warning(
                "turn produced no answer; using fallback",
                model=model,
                stopped=stopped,
                reasoned=reasoned,
                nudged=nudged,
            )
            content = _EMPTY_ANSWER_FALLBACK
        return AgentTurn(
            content=content,
            tools_used=tools_used,
            stopped=stopped,
            entity_refs=refs.refs,
            activity=activity(),
            usage=usage,
        )

    async def _document_written_by(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """What this call is writing, if its module annotated it (#541, ADR-0100/0101).

        Strictly best-effort and never fatal: the document pane is an affordance beside the
        answer, so a slow, broken, or un-annotated lookup costs the user a pane, never the
        turn. Returns ``None`` whenever there is nothing trustworthy to show.
        """
        if self._documents is None:
            return None
        try:
            found = await self._documents(name)
        except Exception as exc:  # a registry hiccup must not take the turn down with it
            log.debug("document annotation lookup failed", tool=name, error=str(exc))
            return None
        if found is None:
            return None
        module, spec = found
        return _document_payload(module, spec, arguments)

    async def _invoke(
        self, name: str, arguments: dict[str, Any], route: dict[str, str], *, tenant: str
    ) -> tuple[str, bool]:
        """Run a tool call, returning ``(text_for_model, is_error)``.

        ``is_error`` is the structural failure signal the activity timeline classifies on:
        True when the call could not be made (unknown tool), the tool reported failure — an
        MCP ``isError`` response the host raises as :class:`ToolCallError` (#435) — the module
        was unreachable (:class:`ModuleUnreachableError`, #472), or an unexpected exception. It
        is tracked from the catch state, *not* sniffed from the returned text: the ToolCallError
        path hands the model the tool's own message verbatim, which need not begin with
        ``error:`` (#440), so a text prefix is not a reliable signal.
        """
        url = route.get(name)
        if url is None:
            return f"error: unknown tool {name!r}", True
        try:
            return await self._mcp.call(name, arguments, url, tenant=tenant), False
        except ToolCallError as exc:
            # The tool ran and reported failure; hand the model the tool's own error
            # text — exactly what it received before the host raised on isError (#435).
            log.warning("tool reported failure", tool=name, error=str(exc))
            return str(exc), True
        except ModuleUnreachableError as exc:
            # The module never answered (down/restarting/timed out). Tell the model plainly
            # so it can retry later or route around it, rather than crash the turn (#472).
            log.warning("tool module unreachable", tool=name, error=str(exc))
            return f"error: tool {name!r} is unavailable: {exc}", True
        except Exception as exc:  # surface the failure to the model, don't crash the turn
            log.warning("tool call failed", tool=name, error=str(exc))
            return f"error: tool {name!r} failed: {exc}", True
