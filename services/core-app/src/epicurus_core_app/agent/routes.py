"""HTTP surface for the agent and its conversations, under /platform/v1/agent."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from epicurus_core import get_logger
from epicurus_core_app.agent.agent import Agent, AgentEvent, AgentTurn
from epicurus_core_app.agent.attachment_sink import AttachmentSink
from epicurus_core_app.agent.builtins import ASK_USER_TOOL
from epicurus_core_app.agent.live_runs import (
    LiveRun,
    LiveRunRegistry,
    RunAlreadyActiveError,
    RunStreamFactory,
)
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.llm.models import ChatMessage
from epicurus_core_app.memory.memory import Memory, MemoryItem
from epicurus_core_app.memory.store import AttachmentStore, MessageRecord, SessionSummary
from epicurus_core_app.readiness import ReadinessProbe

log = get_logger("epicurus_core_app.agent.routes")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Tell buffering proxies (the web container's nginx) to pass events through.
    "X-Accel-Buffering": "no",
}

# How long the in-stream readiness probe may run before we stop waiting and start the
# answer. A slow or still-booting module must never delay the first token (ADR-0027).
READINESS_BUDGET_S = 2.0

# Chat-upload limits (#175) — used when a caller (production wiring) passes no override.
DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_ALLOWED_UPLOAD_TYPES: tuple[str, ...] = (
    "text/*",
    "image/*",
    "application/pdf",
    "application/json",
)


def _content_type_allowed(content_type: str, allowed: Sequence[str]) -> bool:
    """Whether *content_type* matches the allowlist (supports ``type/*`` and ``*/*``)."""
    ct = content_type.split(";", 1)[0].strip().lower()
    if not ct:
        return False
    for rule in allowed:
        if rule in ("*/*", ct):
            return True
        if rule.endswith("/*") and ct.startswith(rule[:-1]):
            return True
    return False


class AgentRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None
    # Opt into cross-chat memory: persist this turn and recall prior context.
    session_id: str | None = None


class RegenerateRequest(BaseModel):
    """Body for POST /sessions/{id}/regenerate — re-answer the last user turn."""

    model: str | None = None


class EditRequest(BaseModel):
    """Body for POST /sessions/{id}/edit — replace the last user message, then re-answer."""

    content: str
    model: str | None = None


class ResumeRequest(BaseModel):
    """Body for POST /runs/{run_id}/resume — the user's answer to an ``ask_user`` question."""

    answer: str


class AttachmentUploaded(BaseModel):
    """The handle the composer keeps for an uploaded file (ADR-0019)."""

    att_id: str
    title: str
    kind: str


class MemoryListing(BaseModel):
    """A page of remembered facts plus the corpus total (so the UI can show the rest)."""

    items: list[MemoryItem]
    total: int


class ActiveRunInfo(BaseModel):
    """An in-flight run a client can re-attach to (from GET /sessions/{id}/active-run, #376).

    ``last_seq`` is the buffer's current end (informational); the client re-attaches with its
    *own* last-seen seq so a reload (which has seen nothing) replays the turn from the start.
    """

    run_id: str
    last_seq: int


def _sse(event: AgentEvent, *, seq: int | None = None) -> str:
    """Frame one event as SSE. ``seq`` (a live-run sequence) becomes the ``id:`` line so a
    client can re-attach with ``Last-Event-ID`` / ``after_seq`` (#376); readiness frames,
    which are per-connection and never replayed, carry no id."""
    prefix = f"id: {seq}\n" if seq is not None else ""
    return f"{prefix}event: {event.type}\ndata: {event.model_dump_json(exclude_none=True)}\n\n"


def create_agent_router(
    agent: Agent,
    memory: Memory,
    tenant: str,
    attachments: AttachmentStore,
    sink: AttachmentSink | None = None,
    probe: ReadinessProbe | None = None,
    *,
    suspended: SuspendedRunStore | None = None,
    live_runs: LiveRunRegistry | None = None,
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

    @router.post("/chat", response_model=AgentTurn)
    async def chat(request: AgentRequest) -> AgentTurn:
        return await agent.run(request.messages, model=request.model, session_id=request.session_id)

    @router.post("/chat/stream")
    async def chat_stream(request: AgentRequest) -> StreamingResponse:
        """The same turn as ``/chat``, streamed as SSE, and durable (#376).

        Runs the turn in a detached task and streams it: ``readiness`` (warming, best-effort
        and time-boxed) then ``delta`` / ``thinking`` / ``tool`` / ``done`` / ``error`` (each
        carrying an ``id:`` seq). A client disconnect ends only this subscriber — the turn runs
        on and persists; the client re-attaches via ``GET /runs/{id}/stream`` (ADR-0027/0055).
        """
        return await _start_turn_response(
            lambda: agent.run_stream(
                request.messages, model=request.model, session_id=request.session_id
            ),
            session_id=request.session_id,
            readiness_model=request.model,
        )

    @router.get("/sessions", response_model=list[SessionSummary])
    async def sessions() -> list[SessionSummary]:
        return await memory.sessions(tenant=tenant)

    @router.get("/sessions/{session_id}", response_model=list[MessageRecord])
    async def session_messages(session_id: str) -> list[MessageRecord]:
        return await memory.messages(tenant=tenant, session_id=session_id)

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, int]:
        removed = await memory.forget(tenant=tenant, session_id=session_id)
        return {"deleted": removed}

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
        return await _start_turn_response(
            lambda: agent.run_stream(
                [], model=request.model, session_id=session_id, persist_input=False
            ),
            session_id=session_id,
            readiness_model=request.model,
        )

    @router.post("/sessions/{session_id}/edit")
    async def edit(session_id: str, request: EditRequest) -> StreamingResponse:
        """Replace the last user message with ``content`` and re-answer it, streamed (#302).

        Edits in place (not a branch): the last user turn's text is updated and re-indexed,
        everything after it is truncated, then a fresh — and durable (#376) — turn streams.
        Emits an ``error`` event if there's no user turn or the new content is empty."""
        content = request.content.strip()
        last_user = await memory.last_user_message_id(tenant=tenant, session_id=session_id)
        if last_user is None or not content:
            return _one_off(AgentEvent(type="error", detail="nothing to edit"))
        await memory.revise_message(
            tenant=tenant, session_id=session_id, message_id=last_user, content=content
        )
        await memory.truncate_after(tenant=tenant, session_id=session_id, after_id=last_user)
        return await _start_turn_response(
            lambda: agent.run_stream(
                [], model=request.model, session_id=session_id, persist_input=False
            ),
            session_id=session_id,
            readiness_model=request.model,
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
