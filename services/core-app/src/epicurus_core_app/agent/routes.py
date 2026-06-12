"""HTTP surface for the agent and its conversations, under /platform/v1/agent."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from epicurus_core_app.agent.agent import Agent, AgentEvent, AgentTurn
from epicurus_core_app.llm.models import ChatMessage
from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.memory.store import MessageRecord, SessionSummary

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Tell buffering proxies (the web container's nginx) to pass events through.
    "X-Accel-Buffering": "no",
}


class AgentRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None
    # Opt into cross-chat memory: persist this turn and recall prior context.
    session_id: str | None = None


def _sse(event: AgentEvent) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json(exclude_none=True)}\n\n"


def create_agent_router(agent: Agent, memory: Memory, tenant: str) -> APIRouter:
    """The agent turn endpoints plus the conversation (session) surface."""
    router = APIRouter(prefix="/platform/v1/agent", tags=["agent"])

    @router.post("/chat", response_model=AgentTurn)
    async def chat(request: AgentRequest) -> AgentTurn:
        return await agent.run(request.messages, model=request.model, session_id=request.session_id)

    @router.post("/chat/stream")
    async def chat_stream(request: AgentRequest) -> StreamingResponse:
        """The same turn as ``/chat``, streamed as SSE (delta / tool / done / error)."""

        async def events() -> AsyncIterator[str]:
            async for event in agent.run_stream(
                request.messages, model=request.model, session_id=request.session_id
            ):
                yield _sse(event)

        return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)

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

    return router
