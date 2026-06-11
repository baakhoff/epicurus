"""HTTP surface for the agent, under /platform/v1/agent."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from epicurus_core_app.agent.agent import Agent, AgentTurn
from epicurus_core_app.llm.models import ChatMessage


class AgentRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None


def create_agent_router(agent: Agent) -> APIRouter:
    """The agent turn endpoint."""
    router = APIRouter(prefix="/platform/v1/agent", tags=["agent"])

    @router.post("/chat", response_model=AgentTurn)
    async def chat(request: AgentRequest) -> AgentTurn:
        return await agent.run(request.messages, model=request.model)

    return router
