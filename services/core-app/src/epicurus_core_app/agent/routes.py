"""HTTP surface for the agent and its conversations, under /platform/v1/agent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from epicurus_core import get_logger
from epicurus_core_app.agent.agent import Agent, AgentEvent, AgentTurn
from epicurus_core_app.agent.attachment_sink import AttachmentSink
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


class AttachmentUploaded(BaseModel):
    """The handle the composer keeps for an uploaded file (ADR-0019)."""

    att_id: str
    title: str
    kind: str


class MemoryListing(BaseModel):
    """A page of remembered facts plus the corpus total (so the UI can show the rest)."""

    items: list[MemoryItem]
    total: int


def _sse(event: AgentEvent) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json(exclude_none=True)}\n\n"


def create_agent_router(
    agent: Agent,
    memory: Memory,
    tenant: str,
    attachments: AttachmentStore,
    sink: AttachmentSink | None = None,
    probe: ReadinessProbe | None = None,
    *,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    allowed_upload_types: Sequence[str] = DEFAULT_ALLOWED_UPLOAD_TYPES,
) -> APIRouter:
    """The agent turn endpoints plus the conversation (session) surface.

    ``sink`` (when configured) durably persists uploads to the storage module; passing
    ``None`` keeps uploads core-side only (e.g. a build without the storage module).
    ``probe`` (when configured) leads a streamed turn with ``readiness`` events so the UI
    can show warming progress before the first token (ADR-0027).

    ``max_upload_bytes`` / ``allowed_upload_types`` bound the upload route (#175).
    """
    router = APIRouter(prefix="/platform/v1/agent", tags=["agent"])

    async def _turn_events(
        messages: list[ChatMessage],
        *,
        model: str | None,
        session_id: str | None,
        persist_input: bool = True,
    ) -> AsyncIterator[str]:
        """Lead with time-boxed ``readiness`` (ADR-0027), then stream the turn as SSE.

        Shared by the live send and the regenerate/edit re-runs (#302); the latter pass
        ``messages=[]`` with ``persist_input=False`` to re-answer the stored tail.
        """
        if probe is not None:
            try:
                async with asyncio.timeout(READINESS_BUDGET_S):
                    async for snap in probe.stream(model=model, tenant_id=tenant):
                        yield _sse(AgentEvent(type="readiness", readiness=snap))
            except TimeoutError:  # a slow probe must not delay the answer
                log.info("readiness probe slow; proceeding to the turn")
            except Exception as exc:  # readiness is an enhancement, never a hard dependency
                log.warning("readiness probe failed; proceeding", error=str(exc))
        async for event in agent.run_stream(
            messages, model=model, session_id=session_id, persist_input=persist_input
        ):
            yield _sse(event)

    @router.post("/chat", response_model=AgentTurn)
    async def chat(request: AgentRequest) -> AgentTurn:
        return await agent.run(request.messages, model=request.model, session_id=request.session_id)

    @router.post("/chat/stream")
    async def chat_stream(request: AgentRequest) -> StreamingResponse:
        """The same turn as ``/chat``, streamed as SSE.

        Leads with ``readiness`` events (warming progress, best-effort and time-boxed),
        then ``delta`` / ``tool`` / ``done`` / ``error`` for the turn itself (ADR-0027).
        """
        return StreamingResponse(
            _turn_events(request.messages, model=request.model, session_id=request.session_id),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
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

    @router.post("/sessions/{session_id}/regenerate")
    async def regenerate(session_id: str, request: RegenerateRequest) -> StreamingResponse:
        """Re-answer the session's last user turn, dropping the previous answer (#302).

        Truncates everything after the last user message (the stale answer + any trailing
        turns, from history and recall), then streams a fresh turn — same SSE protocol as
        ``/chat/stream``. Emits an ``error`` event if there's no user turn to answer."""

        async def events() -> AsyncIterator[str]:
            last_user = await memory.last_user_message_id(tenant=tenant, session_id=session_id)
            if last_user is None:
                yield _sse(AgentEvent(type="error", detail="nothing to regenerate"))
                return
            await memory.truncate_after(tenant=tenant, session_id=session_id, after_id=last_user)
            async for chunk in _turn_events(
                [], model=request.model, session_id=session_id, persist_input=False
            ):
                yield chunk

        return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)

    @router.post("/sessions/{session_id}/edit")
    async def edit(session_id: str, request: EditRequest) -> StreamingResponse:
        """Replace the last user message with ``content`` and re-answer it, streamed (#302).

        Edits in place (not a branch): the last user turn's text is updated and re-indexed,
        everything after it is truncated, then a fresh turn streams. Emits an ``error`` event
        if there's no user turn or the new content is empty."""

        async def events() -> AsyncIterator[str]:
            content = request.content.strip()
            last_user = await memory.last_user_message_id(tenant=tenant, session_id=session_id)
            if last_user is None or not content:
                yield _sse(AgentEvent(type="error", detail="nothing to edit"))
                return
            await memory.revise_message(
                tenant=tenant, session_id=session_id, message_id=last_user, content=content
            )
            await memory.truncate_after(tenant=tenant, session_id=session_id, after_id=last_user)
            async for chunk in _turn_events(
                [], model=request.model, session_id=session_id, persist_input=False
            ):
                yield chunk

        return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)

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
