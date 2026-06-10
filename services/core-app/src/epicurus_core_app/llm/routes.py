"""HTTP surface for the LLM gateway and power control, under /platform/v1."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.models import (
    ChatMessage,
    ChatResult,
    ModelInfo,
    PowerState,
    ProviderInfo,
)
from epicurus_core_app.llm.power import PowerController


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None


class PullRequest(BaseModel):
    model: str


class PowerRequest(BaseModel):
    state: PowerState


class PowerStatus(BaseModel):
    state: PowerState


def create_llm_router(gateway: LlmGateway) -> APIRouter:
    """Routes that expose the LLM gateway to modules and the UI."""
    router = APIRouter(prefix="/platform/v1/llm", tags=["llm"])

    @router.post("/chat", response_model=ChatResult)
    async def chat(request: ChatRequest) -> ChatResult:
        return await gateway.chat(request.messages, model=request.model)

    @router.get("/models", response_model=list[ModelInfo])
    async def list_models() -> list[ModelInfo]:
        return await gateway.models()

    @router.get("/providers", response_model=list[ProviderInfo])
    async def list_providers() -> list[ProviderInfo]:
        return await gateway.providers()

    @router.post("/pull")
    async def pull(request: PullRequest) -> dict[str, str]:
        await gateway.pull(request.model)
        return {"status": "ok", "model": request.model}

    return router


def create_power_router(gateway: LlmGateway, power: PowerController) -> APIRouter:
    """Routes for the main-page power toggle (ADR-0005)."""
    router = APIRouter(prefix="/platform/v1", tags=["power"])

    @router.get("/power", response_model=PowerStatus)
    def get_power() -> PowerStatus:
        return PowerStatus(state=power.state)

    @router.put("/power", response_model=PowerStatus)
    async def set_power(request: PowerRequest) -> PowerStatus:
        if request.state is PowerState.PAUSED:
            power.pause()
            await gateway.unload()
        else:
            power.resume()
        return PowerStatus(state=power.state)

    return router
