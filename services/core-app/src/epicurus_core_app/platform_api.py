"""The module-facing **platform API** (module → core), versioned under ``/platform/v1``.

Modules reach core capabilities — secrets, events, storage, the agent / LLM gateway,
the tool registry — through this local-only API (ADR-0004), rather than wiring to the
backends themselves.  Modules use the typed ``PlatformClient`` from ``epicurus_core``
to call these endpoints without holding provider credentials or SDK dependencies
(ADR-0010).

Endpoints
---------
GET  /platform/v1/info   — discovery: contract version, core version, tenant.
POST /platform/v1/embed  — embed texts via the LLM gateway (returns float vectors).
POST /platform/v1/chat   — chat completion via the LLM gateway.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from epicurus_core import CONTRACT_VERSION, __version__
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.settings import CoreAppSettings


class PlatformInfo(BaseModel):
    """What a module learns about the core it is talking to."""

    contract_version: str
    core_version: str
    tenant: str


class EmbedRequest(BaseModel):
    """Request body for ``POST /platform/v1/embed``."""

    texts: list[str]
    model: str | None = None
    tenant_id: str | None = None


class EmbedResponse(BaseModel):
    """Embedding vectors — one per input text."""

    embeddings: list[list[float]]


class PlatformChatRequest(BaseModel):
    """Request body for ``POST /platform/v1/chat``."""

    messages: list[ChatMessage]
    model: str | None = None
    tools: list[dict[str, Any]] | None = None
    tenant_id: str | None = None


def create_platform_router(settings: CoreAppSettings, gateway: LlmGateway) -> APIRouter:
    """Build the ``/platform/v1`` router that modules call into."""
    router = APIRouter(prefix="/platform/v1", tags=["platform"])

    @router.get("/info", response_model=PlatformInfo)
    def info() -> PlatformInfo:
        return PlatformInfo(
            contract_version=CONTRACT_VERSION,
            core_version=__version__,
            tenant=settings.default_tenant_id,
        )

    @router.post("/embed", response_model=EmbedResponse)
    async def embed(request: EmbedRequest) -> EmbedResponse:
        """Embed texts via the core's LLM gateway.

        The model defaults to the core's configured embedding model when omitted.
        Keys never leave the core; usage is metered via NATS.
        """
        model = request.model or settings.memory_embed_model
        embeddings = await gateway.embed(request.texts, model=model, tenant_id=request.tenant_id)
        return EmbedResponse(embeddings=embeddings)

    @router.post("/chat", response_model=ChatResult)
    async def chat(request: PlatformChatRequest) -> ChatResult:
        """Chat completion via the core's LLM gateway.

        The single module-facing chat path (ADR-0021): the core owns model
        selection, fallback, key management, and usage accounting — the module
        provides only messages and optional overrides. Returns the shared
        ``ChatResult``.
        """
        return await gateway.chat(
            request.messages,
            model=request.model,
            tools=request.tools,
            tenant_id=request.tenant_id,
        )

    return router
