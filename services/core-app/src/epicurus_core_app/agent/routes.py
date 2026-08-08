"""HTTP surface for the agent and its conversations, under /platform/v1/agent."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from epicurus_core import get_logger
from epicurus_core_app.agent.agent import ASK_APPROVAL_TOOL, Agent, AgentEvent, AgentTurn
from epicurus_core_app.agent.attachment_sink import AttachmentSink
from epicurus_core_app.agent.builtins import ASK_USER_TOOL
from epicurus_core_app.agent.live_runs import (
    LiveRun,
    LiveRunRegistry,
    RunAlreadyActiveError,
    RunStreamFactory,
)
from epicurus_core_app.agent.pending_approvals import PendingApprovalStore
from epicurus_core_app.agent.pending_drafts import PendingDraft, PendingDraftStore
from epicurus_core_app.agent.session_delete import DeletedSession, SessionDeleteCascade
from epicurus_core_app.agent.session_model import SessionModelStore
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.automations.store import AutomationSessionStore, SessionMeta
from epicurus_core_app.llm.models import ChatMessage
from epicurus_core_app.memory.memory import Memory, MemoryItem
from epicurus_core_app.memory.profile import SOURCE_EDITED, StandingProfile, StandingProfileStore
from epicurus_core_app.memory.store import AttachmentStore, MessageRecord, SessionSummary
from epicurus_core_app.readiness import ReadinessProbe

# Chat-upload limits (#175) — shared with the Files-page upload (#479). Re-exported under
# the old names so existing imports (tests included) keep working.
from epicurus_core_app.upload_limits import (
    DEFAULT_ALLOWED_UPLOAD_TYPES,
    DEFAULT_MAX_UPLOAD_BYTES,
)
from epicurus_core_app.upload_limits import (
    content_type_allowed as _content_type_allowed,
)

log = get_logger("epicurus_core_app.agent.routes")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Tell buffering proxies (the web container's nginx) to pass events through.
    "X-Accel-Buffering": "no",
}

# How long the in-stream readiness probe may run before we stop waiting and start the
# answer. A slow or still-booting module must never delay the first token (ADR-0027).
READINESS_BUDGET_S = 2.0


class AgentRequest(BaseModel):
    messages: list[ChatMessage]
    # The caller's *default* model, not an override (#707, ADR-0111). A session that has been
    # given a model — by the picker or by `set_chat_model` — runs that one, and this is used
    # only while it has none. See `_turn_model`.
    model: str | None = None
    # Opt into cross-chat memory: persist this turn and recall prior context.
    session_id: str | None = None


class RegenerateRequest(BaseModel):
    """Body for POST /sessions/{id}/regenerate — re-answer the last user turn.

    ``model`` is the caller's default, deferring to the session's own model when it has one
    (#707) — re-answering never silently switches a conversation's model.
    """

    model: str | None = None


class EditRequest(BaseModel):
    """Body for POST /sessions/{id}/edit — replace a user message, then re-answer.

    ``message_id`` names the turn to revise (#552); omitted, it defaults to the session's last
    user message — the only turn #302 could edit, so callers predating this field are unchanged.
    """

    content: str
    model: str | None = None
    message_id: int | None = None


class SetSessionModelBody(BaseModel):
    """Body for PUT /sessions/{id}/model — an explicit picker change (#707).

    Writes the same field `set_chat_model` does, so a tool-set choice and a picker change
    can never fight: whichever happens last is what's persisted. ``model: null`` clears the
    override (picking "core default" back in the picker) — the tool itself never clears,
    only ever sets, so this is the picker's own escape hatch.
    """

    model: str | None


class ResumeRequest(BaseModel):
    """Body for POST /runs/{run_id}/resume — the user's answer to an ``ask_user`` question."""

    answer: str


class DraftDecision(BaseModel):
    """Body for POST /runs/{run_id}/draft — the operator's Confirm/Decline of a draft (ADR-0085).

    ``send`` transmits the reviewed draft; ``decline`` sends nothing. ``reason`` is an optional
    short note carried back to the model on Decline for steering (ignored on ``send``).
    """

    decision: Literal["send", "decline"]
    reason: str | None = None


class ApprovalDecision(BaseModel):
    """Body for POST /runs/{run_id}/approval — the operator's Approve/Reject of an
    ``ask_approval`` pause (#745, ADR-0117).

    Unlike :class:`DraftDecision`, resolving this never itself calls a module: by the time this
    posts, the web client has already called the relevant module's own review-decision endpoint
    directly (the agent must never approve its own work — see ``suggestions.py``) — this only
    resumes the paused turn with the outcome. ``comment`` is an optional short note carried back
    to the model either way, e.g. the operator's reason for a Reject.
    """

    decision: Literal["approved", "rejected"]
    comment: str | None = None


# Transmit a confirmed draft via a module's ``POST /send`` → the provider message id, or raise
# ``HTTPException`` with the module's hint (ADR-0085). Wired to ``ModuleRegistry.send_draft``.
SendDraft = Callable[[str, dict[str, Any]], Awaitable[str]]


class AttachmentUploaded(BaseModel):
    """The handle the composer keeps for an uploaded file (ADR-0019)."""

    att_id: str
    title: str
    kind: str


class MemoryListing(BaseModel):
    """A page of remembered facts plus the corpus total (so the UI can show the rest)."""

    items: list[MemoryItem]
    total: int


class ProfileView(BaseModel):
    """The standing profile for the memory view (#527, ADR-0094).

    ``profile`` is ``None`` when none has been synthesized yet (the agent then behaves exactly as
    before). ``source`` is ``auto`` (nightly synthesis) or ``edited`` (an operator correction, which
    survives re-synthesis); ``pinned`` restates that as a plain flag the UI can badge. ``versions``
    is the recent history, newest first, so the operator can see how it evolved.
    """

    profile: StandingProfile | None
    source: str | None = None
    pinned: bool = False
    versions: list[StandingProfile] = []


class ProfileBody(BaseModel):
    """Body for PUT /memory/profile — the operator's edited standing profile."""

    content: str


class ActiveRunInfo(BaseModel):
    """An in-flight run a client can re-attach to (from GET /sessions/{id}/active-run, #376).

    ``last_seq`` is the buffer's current end (informational); the client re-attaches with its
    *own* last-seen seq so a reload (which has seen nothing) replays the turn from the start.
    """

    run_id: str
    last_seq: int


class ActiveSessions(BaseModel):
    """Session ids with an in-flight turn — drives the conversations-list running indicator
    (#396). Point-in-time and best-effort (the live-run buffer is a disposable cache)."""

    session_ids: list[str]


def _sse(event: AgentEvent, *, seq: int | None = None) -> str:
    """Frame one event as SSE. ``seq`` (a live-run sequence) becomes the ``id:`` line so a
    client can re-attach with ``Last-Event-ID`` / ``after_seq`` (#376); readiness frames,
    which are per-connection and never replayed, carry no id."""
    prefix = f"id: {seq}\n" if seq is not None else ""
    return f"{prefix}event: {event.type}\ndata: {event.model_dump_json(exclude_none=True)}\n\n"


def _with_automation(summary: SessionSummary, meta: SessionMeta | None) -> SessionSummary:
    """Stamp an automation's session with its badge/grouping metadata (#672); unchanged if none."""
    if meta is None:
        return summary
    return summary.model_copy(
        update={
            "automation_id": meta.automation_id,
            "automation_name": meta.name,
            "chat_mode": meta.chat_mode,
        }
    )


def _with_model(summary: SessionSummary, model: str | None) -> SessionSummary:
    """Stamp a session with its persisted model override (#707); unchanged if none."""
    if model is None:
        return summary
    return summary.model_copy(update={"model": model})


def create_agent_router(
    agent: Agent,
    memory: Memory,
    tenant: str,
    attachments: AttachmentStore,
    sink: AttachmentSink | None = None,
    probe: ReadinessProbe | None = None,
    *,
    suspended: SuspendedRunStore | None = None,
    pending_drafts: PendingDraftStore | None = None,
    pending_approvals: PendingApprovalStore | None = None,
    send_draft: SendDraft | None = None,
    live_runs: LiveRunRegistry | None = None,
    profile: StandingProfileStore | None = None,
    automation_sessions: AutomationSessionStore | None = None,
    session_models: SessionModelStore | None = None,
    session_delete: SessionDeleteCascade | None = None,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    allowed_upload_types: Sequence[str] = DEFAULT_ALLOWED_UPLOAD_TYPES,
) -> APIRouter:
    """The agent turn endpoints plus the conversation (session) surface.

    ``sink`` (when configured) durably persists uploads to the storage module; passing
    ``None`` keeps uploads core-side only (e.g. a build without the storage module).
    ``probe`` (when configured) leads a streamed turn with ``readiness`` events so the UI
    can show warming progress before the first token (ADR-0027).

    ``live_runs`` decouples a turn from the request that started it (#376): the turn runs in a
    detached task that buffers its events, so a client disconnect (PWA backgrounded, refresh)
    no longer aborts it — the answer still persists, and a reconnecting client re-attaches.
    Defaults to a private registry when unset (production passes the shared one so its reaper
    runs and re-attach works across requests).

    ``max_upload_bytes`` / ``allowed_upload_types`` bound the upload route (#175).
    """
    router = APIRouter(prefix="/platform/v1/agent", tags=["agent"])
    runs = live_runs if live_runs is not None else LiveRunRegistry()

    async def _readiness_prelude(model: str | None) -> AsyncIterator[str]:
        """Time-boxed ``readiness`` frames that lead a *fresh* turn (ADR-0027).

        Per-connection and never buffered into the run: a client that re-attaches later must
        not replay a stale warming bar (#376), so these carry no ``id:`` and the re-attach
        endpoint omits them entirely.
        """
        if probe is None:
            return
        try:
            async with asyncio.timeout(READINESS_BUDGET_S):
                async for snap in probe.stream(model=model, tenant_id=tenant):
                    yield _sse(AgentEvent(type="readiness", readiness=snap))
        except TimeoutError:  # a slow probe must not delay the answer
            log.info("readiness probe slow; proceeding to the turn")
        except Exception as exc:  # readiness is an enhancement, never a hard dependency
            log.warning("readiness probe failed; proceeding", error=str(exc))

    async def _stream_run(run: LiveRun, after_seq: int) -> AsyncIterator[str]:
        """Replay the run's buffer after ``after_seq``, then tail it live, as SSE frames."""
        async for seq, event in run.subscribe(after_seq):
            yield _sse(event, seq=seq)

    async def _start_turn_response(
        factory: RunStreamFactory,
        *,
        session_id: str | None,
        readiness_model: str | None,
        lead_readiness: bool = True,
    ) -> StreamingResponse:
        """Start a detached turn and return its subscribing SSE response.

        A turn already running for this session yields a 409 (+ ``X-Run-Id``) so a duplicate
        caller re-attaches instead of racing a second turn. The subscriber streams from seq 0,
        so any events buffered while readiness streamed are replayed — none are missed.
        """
        try:
            run = await runs.start(factory, tenant=tenant, session_id=session_id)
        except RunAlreadyActiveError as exc:
            raise HTTPException(
                status_code=409, detail=str(exc), headers={"X-Run-Id": exc.run_id}
            ) from exc

        async def events() -> AsyncIterator[str]:
            if lead_readiness:
                async for frame in _readiness_prelude(readiness_model):
                    yield frame
            async for frame in _stream_run(run, 0):
                yield frame

        return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)

    def _one_off(event: AgentEvent) -> StreamingResponse:
        """A single-frame SSE response (a pre-turn ``error``, or the ``gone`` sentinel)."""

        async def one() -> AsyncIterator[str]:
            yield _sse(event)

        return StreamingResponse(one(), media_type="text/event-stream", headers=SSE_HEADERS)

    async def _turn_model(request_model: str | None, session_id: str | None) -> str | None:
        """The model a turn actually runs on (#707, ADR-0111).

        The session row is the owner of truth: once a conversation has been given a model — by
        the picker or by ``set_chat_model`` — every turn on it runs that model, whoever sends
        the turn and whatever they believe the model to be. ``model`` on the request is the
        *caller's default*, used only while the session has no choice of its own: a brand-new
        chat, or one whose override was cleared back to the core default.

        Resolving here rather than in the client is what makes the tool trustworthy. A client
        computing this itself reads a cached sessions list, so a turn sent in the window between
        the tool writing the row and that cache refreshing would silently run the *old* model —
        the agent says it switched, and the next answer proves otherwise. Nothing a caller does
        can open that window now, and a headless run or a future client inherits the behaviour
        without having to reimplement it.

        Never raises: a store hiccup degrades to the caller's default, which is exactly the
        pre-override behaviour, rather than costing the user their turn.
        """
        if session_id is None or session_models is None:
            return request_model
        try:
            stored = await session_models.get(tenant=tenant, session_id=session_id)
        except Exception as exc:
            log.warning("session model lookup failed", session_id=session_id, error=str(exc))
            return request_model
        return stored or request_model

    @router.post("/chat", response_model=AgentTurn)
    async def chat(request: AgentRequest) -> AgentTurn:
        model = await _turn_model(request.model, request.session_id)
        return await agent.run(request.messages, model=model, session_id=request.session_id)

    @router.post("/chat/stream")
    async def chat_stream(request: AgentRequest) -> StreamingResponse:
        """The same turn as ``/chat``, streamed as SSE, and durable (#376).

        Runs the turn in a detached task and streams it: ``readiness`` (warming, best-effort
        and time-boxed) then ``delta`` / ``thinking`` / ``tool`` / ``done`` / ``error`` (each
        carrying an ``id:`` seq). A client disconnect ends only this subscriber — the turn runs
        on and persists; the client re-attaches via ``GET /runs/{id}/stream`` (ADR-0027/0055).
        """
        model = await _turn_model(request.model, request.session_id)
        return await _start_turn_response(
            lambda: agent.run_stream(request.messages, model=model, session_id=request.session_id),
            session_id=request.session_id,
            readiness_model=model,
        )

    @router.get("/sessions", response_model=list[SessionSummary])
    async def sessions() -> list[SessionSummary]:
        summaries = await memory.sessions(tenant=tenant)
        # Badge/group automation chats (#672) and stamp each session's persisted model
        # override (#707) — enriched here, not in the ConversationStore, so that store stays
        # automations/model-agnostic. Each lookup is independently best-effort: a hiccup in
        # one degrades that enrichment only, rather than emptying the list.
        session_ids = [s.id for s in summaries]
        if automation_sessions is not None:
            try:
                metas = await automation_sessions.lookup(tenant=tenant, session_ids=session_ids)
                summaries = [_with_automation(s, metas.get(s.id)) for s in summaries]
            except Exception as exc:  # never fail the chat list over the badge lookup
                log.warning("automation session enrichment failed", error=str(exc))
        if session_models is not None:
            try:
                models = await session_models.lookup(tenant=tenant, session_ids=session_ids)
                summaries = [_with_model(s, models.get(s.id)) for s in summaries]
            except Exception as exc:  # never fail the chat list over the model lookup
                log.warning("session model enrichment failed", error=str(exc))
        return summaries

    @router.get("/sessions/{session_id}", response_model=list[MessageRecord])
    async def session_messages(session_id: str) -> list[MessageRecord]:
        return await memory.messages(tenant=tenant, session_id=session_id)

    @router.delete("/sessions/{session_id}", response_model=DeletedSession)
    async def delete_session(session_id: str) -> DeletedSession:
        """Delete a conversation — everything it produced, everywhere (#771).

        A full cascade, not just the message rows: attachments, still-queued extraction
        exchanges (so the nightly drain can't distil a deleted chat), the session-model row,
        suspended/pending paused runs, the automation badge mapping, and any in-flight run's
        buffered state. Facts already extracted on *previous* nights are curated memory,
        managed in the Memory view — deleting a chat stops all future derivation but does not
        un-learn them. Falls back to a messages-only delete when no cascade is wired (bare
        test routers); the response's ``deleted`` field is the message count either way.
        """
        if session_delete is None:
            removed = await memory.forget(tenant=tenant, session_id=session_id)
            return DeletedSession(deleted=removed)
        return await session_delete.delete(tenant=tenant, session_id=session_id)

    @router.put("/sessions/{session_id}/model")
    async def set_session_model(
        session_id: str, body: SetSessionModelBody
    ) -> dict[str, str | None]:
        """An explicit picker change for this session (#707).

        Writes the same field the ``set_chat_model`` tool does — one owner of truth, so a
        tool-set choice and a picker change can never fight, whichever happens last stands.
        Not validated against the model catalog here: the picker itself only ever offers a
        real name, the same two sources (``GET /llm/models`` + ``GET /llm/saved-models``)
        the tool resolves against. ``model: null`` clears the override (picking "core
        default" back) — the tool itself never clears, only the picker can.
        """
        if session_models is None:
            raise HTTPException(status_code=503, detail="session models are not available")
        if body.model is None:
            await session_models.clear(tenant=tenant, session_id=session_id)
            return {"model": None}
        model = body.model.strip()
        if not model:
            raise HTTPException(status_code=400, detail="model must not be blank")
        await session_models.set(tenant=tenant, session_id=session_id, model=model)
        return {"model": model}

    @router.get("/sessions/{session_id}/active-run", response_model=ActiveRunInfo | None)
    async def active_run(session_id: str) -> ActiveRunInfo | None:
        """The session's in-flight run to re-attach to, or ``null`` if none is live (#376).

        How a client rediscovers a turn after a reload / reconnect: if this returns a run, the
        client re-attaches via ``GET /runs/{id}/stream``; if ``null``, the turn already finished
        (or never was) and the durable transcript (``GET /sessions/{id}``) holds the answer.
        """
        run = runs.active_for_session(tenant=tenant, session_id=session_id)
        if run is None:
            return None
        return ActiveRunInfo(run_id=run.run_id, last_seq=run.last_seq)

    @router.delete("/sessions/{session_id}/active-run")
    async def cancel_active_run(session_id: str) -> dict[str, bool]:
        """Cancel the session's in-flight turn — the explicit ``stop`` (#376).

        A turn is decoupled from the request now, so a disconnect no longer ends it; the client
        calls this on ``stop`` to actually halt it (and free the session for the next send).
        """
        run = runs.active_for_session(tenant=tenant, session_id=session_id)
        if run is None:
            return {"cancelled": False}
        await runs.cancel(run)
        return {"cancelled": True}

    @router.get("/active-runs", response_model=ActiveSessions)
    async def active_runs() -> ActiveSessions:
        """Session ids with an in-flight turn right now — the conversations-list running
        indicator (#396).

        A sibling of ``/sessions/{id}/active-run`` that answers "which sessions are generating"
        in one request, so the conversations list needn't poll each row. Best-effort and
        point-in-time: the live-run buffer is a disposable cache (constraint #2), so a turn may
        finish just after the snapshot and a multi-instance deployment sees only this instance's
        runs (the registry is the seam where a shared event log slots in)."""
        return ActiveSessions(session_ids=runs.active_sessions(tenant=tenant))

    @router.post("/sessions/{session_id}/regenerate")
    async def regenerate(session_id: str, request: RegenerateRequest) -> StreamingResponse:
        """Re-answer the session's last user turn, dropping the previous answer (#302).

        Truncates everything after the last user message (the stale answer + any trailing
        turns, from history and recall), then streams a fresh — and durable (#376) — turn,
        same SSE protocol as ``/chat/stream``. Emits an ``error`` event if there's no user
        turn to answer."""
        last_user = await memory.last_user_message_id(tenant=tenant, session_id=session_id)
        if last_user is None:
            return _one_off(AgentEvent(type="error", detail="nothing to regenerate"))
        await memory.truncate_after(tenant=tenant, session_id=session_id, after_id=last_user)
        model = await _turn_model(request.model, session_id)
        return await _start_turn_response(
            lambda: agent.run_stream([], model=model, session_id=session_id, persist_input=False),
            session_id=session_id,
            readiness_model=model,
        )

    @router.post("/sessions/{session_id}/edit")
    async def edit(session_id: str, request: EditRequest) -> StreamingResponse:
        """Replace a user message with ``content`` and re-answer from there, streamed (#302, #552).

        Edits in place (not a branch): the named turn's text is updated, everything after it is
        truncated, then a fresh — and durable (#376) — turn streams. ``message_id`` selects the
        turn and defaults to the last user message (#302's only target); editing further back
        discards the real turns behind it, which is why the client confirms first.

        Every check runs *before* anything is written, so a rejected edit leaves history exactly
        as it was — a bad anchor must never cost the user the tail of their conversation. Emits
        an ``error`` event when the content is empty, the session has no user turn, the anchor
        isn't a user message of *this* conversation, or a turn is already running (revising under
        a live run would truncate history the run is mid-way through answering).
        """
        content = request.content.strip()
        if not content:
            return _one_off(AgentEvent(type="error", detail="nothing to edit"))
        # Ordered ahead of the write: the one-run guard lives in ``runs.start`` (a 409 from
        # ``_start_turn_response``), which today's flow only reaches *after* revising and
        # truncating — leaving the history cut and unanswered. Checking here keeps the reject
        # side-effect-free. A run starting in the gap still 409s there, unchanged.
        if runs.active_for_session(tenant=tenant, session_id=session_id) is not None:
            return _one_off(
                AgentEvent(type="error", detail="wait for this turn to finish before editing")
            )
        anchor = request.message_id
        if anchor is None:
            anchor = await memory.last_user_message_id(tenant=tenant, session_id=session_id)
            if anchor is None:
                return _one_off(AgentEvent(type="error", detail="nothing to edit"))
        else:
            # Scoped to this session, so an id from another conversation reads as absent.
            role = await memory.message_role(
                tenant=tenant, session_id=session_id, message_id=anchor
            )
            if role is None:
                return _one_off(
                    AgentEvent(type="error", detail="that message is not in this conversation")
                )
            if role != "user":
                return _one_off(
                    AgentEvent(type="error", detail="only your own messages can be edited")
                )
        await memory.revise_message(
            tenant=tenant, session_id=session_id, message_id=anchor, content=content
        )
        await memory.truncate_after(tenant=tenant, session_id=session_id, after_id=anchor)
        model = await _turn_model(request.model, session_id)
        return await _start_turn_response(
            lambda: agent.run_stream([], model=model, session_id=session_id, persist_input=False),
            session_id=session_id,
            readiness_model=model,
        )

    @router.post("/runs/{run_id}/resume")
    async def resume_run(run_id: str, request: ResumeRequest) -> StreamingResponse:
        """Resume a turn paused by ``ask_user``, supplying the user's answer (ADR-0053).

        Takes the suspended run (consuming it), appends the answer as the pending tool call's
        result, and continues the turn as a fresh durable run (#376) — same SSE protocol as
        ``/chat/stream``. Emits an ``error`` event if the run is unknown / expired / already
        answered. (The suspended ``run_id`` here is the DB pause token, distinct from a live
        run's id used for ``/runs/{id}/stream`` re-attach.)"""
        run = None if suspended is None else await suspended.take(tenant=tenant, run_id=run_id)
        if run is None:
            detail = "this question has expired or was already answered"
            return _one_off(AgentEvent(type="error", detail=detail))
        convo = [ChatMessage.model_validate(m) for m in run.conversation]
        convo.append(
            ChatMessage(
                role="tool",
                tool_call_id=run.pending_call_id,
                name=ASK_USER_TOOL,
                content=request.answer,
            )
        )
        return await _start_turn_response(
            lambda: agent.run_stream(
                [], model=run.model, session_id=run.session_id, resume_convo=convo
            ),
            session_id=run.session_id,
            readiness_model=run.model,
            lead_readiness=False,  # a resume continues a warm turn; no warming bar needed
        )

    async def _send_confirmed_draft(run: PendingDraft) -> str:
        """Transmit a confirmed draft and format the tool result the model sees (ADR-0085).

        A module failure (e.g. a Gmail scope / rate-limit 403 relayed as a hint) is caught and
        returned as an ``error:`` tool result rather than failing the resume — the model then tells
        the user it could not send, and the draft is already consumed so they simply re-ask.
        """
        if send_draft is None:
            return "error: sending is unavailable right now; tell the user it was not sent."
        try:
            message_id = await send_draft(run.module, run.draft)
        except HTTPException as exc:
            log.warning("confirmed draft send failed", module=run.module, detail=str(exc.detail))
            return f"error: the message was NOT sent — {exc.detail}"
        return f"Sent. Provider message id: {message_id}."

    @router.post("/runs/{run_id}/draft")
    async def resolve_draft(run_id: str, request: DraftDecision) -> StreamingResponse:
        """Confirm (send) or Decline a draft paused for review (ADR-0085, #563).

        Takes the pending draft (consuming it, so a double-submit can't send twice). On ``send``
        the core transmits it via the module's ``POST /send`` and appends the outcome (``Sent.`` +
        the provider message id, or the module's error hint) as the compose call's tool result; on
        ``decline`` it appends a "not sent" result carrying any reason. Either way the turn
        continues as a fresh durable run (#376) on the same SSE protocol as ``/chat/stream``.
        Emits an ``error`` event if the draft is unknown / expired / already resolved. Confirm and
        Decline are connection-gated client-side (#530); the ``run_id`` is the DB pause token,
        distinct from a live run's id used for ``/runs/{id}/stream`` re-attach."""
        run = (
            None
            if pending_drafts is None
            else await pending_drafts.take(tenant=tenant, run_id=run_id)
        )
        if run is None:
            detail = "this draft has expired or was already resolved"
            return _one_off(AgentEvent(type="error", detail=detail))
        if request.decision == "send":
            content = await _send_confirmed_draft(run)
        else:
            reason = (request.reason or "").strip()
            content = "The user declined to send this draft; it was NOT sent." + (
                f" Their reason: {reason}" if reason else ""
            )
        convo = [ChatMessage.model_validate(m) for m in run.conversation]
        convo.append(
            ChatMessage(
                role="tool", tool_call_id=run.pending_call_id, name=run.tool, content=content
            )
        )
        return await _start_turn_response(
            lambda: agent.run_stream(
                [], model=run.model, session_id=run.session_id, resume_convo=convo
            ),
            session_id=run.session_id,
            readiness_model=run.model,
            lead_readiness=False,
        )

    @router.post("/runs/{run_id}/approval")
    async def resolve_approval(run_id: str, request: ApprovalDecision) -> StreamingResponse:
        """Resume a turn paused by ``ask_approval``, supplying the operator's decision (#745,
        ADR-0117).

        Takes the pending approval (consuming it, so a double-submit can't resume twice) and
        appends the outcome (plus any comment) as the ``ask_approval`` call's tool result. Unlike
        ``resolve_draft``, this never calls a module itself — approve/reject already happened
        against the module's own review-decision endpoint before this posts (the web client calls
        it directly; see ``ApprovalDecision``). Either way the turn continues as a fresh durable
        run (#376) on the same SSE protocol as ``/chat/stream``. Emits an ``error`` event if the
        approval is unknown / expired / already resolved.
        """
        run = (
            None
            if pending_approvals is None
            else await pending_approvals.take(tenant=tenant, run_id=run_id)
        )
        if run is None:
            detail = "this approval has expired or was already resolved"
            return _one_off(AgentEvent(type="error", detail=detail))
        comment = (request.comment or "").strip()
        if request.decision == "approved":
            content = "The operator approved this change."
        else:
            content = "The operator rejected this change."
        if comment:
            content += f" Their comment: {comment}"
        convo = [ChatMessage.model_validate(m) for m in run.conversation]
        convo.append(
            ChatMessage(
                role="tool",
                tool_call_id=run.pending_call_id,
                name=ASK_APPROVAL_TOOL,
                content=content,
            )
        )
        return await _start_turn_response(
            lambda: agent.run_stream(
                [], model=run.model, session_id=run.session_id, resume_convo=convo
            ),
            session_id=run.session_id,
            readiness_model=run.model,
            lead_readiness=False,
        )

    @router.get("/runs/{run_id}/stream")
    async def reattach_run(
        run_id: str, request: Request, after_seq: int = Query(default=0, ge=0)
    ) -> StreamingResponse:
        """Re-attach to an in-flight turn, replaying its buffer after ``after_seq`` (#376).

        Honors a ``Last-Event-ID`` header too (native ``EventSource``), taking the larger of
        the two as the resume point. If the run is unknown, finished-and-reaped, or another
        tenant's, emits a single ``gone`` event so the client falls back to the durable
        transcript (``GET /sessions/{id}``)."""
        start_seq = after_seq
        header_id = request.headers.get("last-event-id")
        if header_id is not None:
            with contextlib.suppress(ValueError):  # a malformed header is ignored, not fatal
                start_seq = max(start_seq, int(header_id))
        run = runs.get(run_id, tenant=tenant)
        if run is None:
            return _one_off(AgentEvent(type="gone", detail="run not found"))
        return StreamingResponse(
            _stream_run(run, start_seq), media_type="text/event-stream", headers=SSE_HEADERS
        )

    @router.get("/memory", response_model=MemoryListing)
    async def list_memory(
        q: str | None = None, limit: int = Query(default=200, ge=1, le=500)
    ) -> MemoryListing:
        """The cross-chat memory corpus — the durable facts the model remembers about the user.

        Without ``q`` it returns the facts newest-first; with ``q`` it returns what recall
        surfaces for that query (the same ranking a chat turn gets). ``total`` is the full
        corpus size. A backend failure surfaces as a 5xx — an inspection view must not mask
        errors; an empty corpus is a clean ``{"items": [], "total": 0}``.
        """
        if q and q.strip():
            items, total = await memory.search_memory(tenant=tenant, query=q.strip(), limit=limit)
        else:
            items, total = await memory.memories(tenant=tenant, limit=limit)
        return MemoryListing(items=items, total=total)

    # NOTE: /memory/profile is declared BEFORE /memory/{memory_id} so a DELETE to it isn't
    # captured as "forget the fact with id 'profile'" (FastAPI matches in declaration order).
    @router.get("/memory/profile", response_model=ProfileView)
    async def get_profile() -> ProfileView:
        """The standing profile the agent injects each turn (#527, ADR-0094), plus its history.

        ``profile`` is ``null`` when none has been synthesized (the agent then behaves as before).
        A backend failure surfaces as a 5xx — an inspection view must not mask errors. Disabled
        (no profile store wired) is a clean empty view, not an error.
        """
        if profile is None:
            return ProfileView(profile=None)
        latest = await profile.latest(tenant=tenant)
        versions = await profile.versions(tenant=tenant, limit=10)
        return ProfileView(
            profile=latest,
            source=latest.source if latest else None,
            pinned=latest is not None and latest.source == SOURCE_EDITED,
            versions=versions,
        )

    @router.put("/memory/profile", response_model=ProfileView)
    async def set_profile(body: ProfileBody) -> ProfileView:
        """Replace the standing profile with an operator edit — pinned, surviving re-synthesis.

        Saved as an ``edited`` version, which the nightly synthesizer preserves (it won't clobber a
        correction). A blank body **clears** the profile instead (resume auto-synthesis) — the same
        as DELETE — so the memory view's editor can reset with an empty field.
        """
        if profile is None:
            raise HTTPException(status_code=503, detail="standing profile is not available")
        if not body.content.strip():
            await profile.clear(tenant=tenant)
            return ProfileView(profile=None)
        saved = await profile.save(tenant=tenant, content=body.content, source=SOURCE_EDITED)
        return ProfileView(profile=saved, source=saved.source, pinned=True, versions=[saved])

    @router.delete("/memory/profile")
    async def clear_profile() -> dict[str, int]:
        """Clear the standing profile (all versions); the next synthesis regenerates a fresh one."""
        if profile is None:
            return {"cleared": 0}
        return {"cleared": await profile.clear(tenant=tenant)}

    @router.delete("/memory/{memory_id}")
    async def forget_memory(memory_id: str) -> dict[str, int]:
        """Forget one remembered fact so it stops being recalled (the conversation is kept)."""
        forgotten = await memory.forget_memory(tenant=tenant, memory_id=memory_id)
        return {"forgotten": forgotten}

    @router.post("/attachments", response_model=AttachmentUploaded)
    async def upload_attachment(file: UploadFile) -> AttachmentUploaded:
        """Upload a file to attach to a chat turn; returns its core-side handle (ADR-0019).

        Rejects an upload whose type is not in the allowlist (415) or whose size exceeds
        ``max_upload_bytes`` (413) before storing it (#175). Otherwise keeps the core-side
        handle (read back to expand the attachment into the turn) and, best-effort, persists
        the bytes to the storage sink so the upload is durably kept and browsable in the
        Files page (ADR-0025). A sink failure never fails the upload.
        """
        kind = file.content_type or "application/octet-stream"
        if not _content_type_allowed(kind, allowed_upload_types):
            raise HTTPException(status_code=415, detail=f"unsupported file type: {kind}")
        over_limit = f"file exceeds the {max_upload_bytes}-byte limit"
        # Starlette sets file.size from the parsed part — reject before reading the spool.
        if file.size is not None and file.size > max_upload_bytes:
            raise HTTPException(status_code=413, detail=over_limit)
        content = await file.read()
        if len(content) > max_upload_bytes:  # defense if size was unset or understated
            raise HTTPException(status_code=413, detail=over_limit)
        title = file.filename or "file"
        att_id = await attachments.save(tenant=tenant, kind=kind, title=title, content=content)
        if sink is not None:
            try:
                await sink.persist(
                    tenant=tenant,
                    att_id=att_id,
                    filename=title,
                    content_type=kind,
                    data=content,
                )
            except Exception as exc:  # durability is best-effort; the upload still stands
                log.warning(
                    "attachment sink persist failed; kept core-side only",
                    att_id=att_id,
                    error=str(exc),
                )
        return AttachmentUploaded(att_id=att_id, title=title, kind=kind)

    return router
