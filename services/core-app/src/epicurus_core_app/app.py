"""The epicurus core runtime service — the brain the platform is built on.

Across Phase 1 this service grows to host the agent loop, the LLM gateway, memory,
the power-state machine, and the MCP host that drives modules' tools. It stands the
container up: the ops surface (``/health`` + ``/metrics``), a connected NATS event
bus, the module-facing **platform API**, and the **LLM gateway** + **power** control.

Unlike a sidecar module (which exposes MCP tools *to* the agent), the core is the
**host** — so it serves a platform API and drives modules, rather than mounting its
own MCP tool server (ADR-0004 / ADR-0009).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import EventBus, SecretStore, add_ops_routes, configure_logging, get_logger
from epicurus_core_app.agent.agent import Agent
from epicurus_core_app.agent.mcp_host import McpHost
from epicurus_core_app.agent.routes import create_agent_router
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.power import GatewayPausedError, PowerController
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.routes import create_llm_router, create_power_router
from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.memory.recall import SemanticRecall
from epicurus_core_app.memory.store import ConversationStore
from epicurus_core_app.modules import ModuleRegistry, create_modules_router
from epicurus_core_app.oauth.routes import create_oauth_router
from epicurus_core_app.oauth.service import OAuthService
from epicurus_core_app.platform_api import create_platform_router
from epicurus_core_app.settings import CoreAppSettings

SERVICE_NAME = "core-app"


def _service_version() -> str:
    """The installed distribution version, for ``/health``."""
    try:
        return pkg_version("epicurus-core-app")
    except PackageNotFoundError:
        return "0.0.0"


def create_app() -> FastAPI:
    """Build the core runtime ASGI app (connects to NATS on startup)."""
    settings = CoreAppSettings(service_name=SERVICE_NAME)
    configure_logging(settings)
    log = get_logger(SERVICE_NAME)
    bus = EventBus.from_settings(settings)
    power = PowerController()
    secrets = SecretStore.from_settings(settings)
    engine = create_async_engine(settings.database_url)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)

    prefs = LlmPrefsStore(engine)
    gateway = LlmGateway(
        ollama_url=settings.ollama_url,
        default_model=settings.llm_default_model,
        keep_alive=settings.llm_keep_alive,
        power=power,
        secrets=secrets,
        default_tenant=settings.default_tenant_id,
        bus=bus,
        fallbacks=settings.fallback_models,
        num_retries=settings.llm_num_retries,
        temperature=settings.llm_temperature,
        top_p=settings.llm_top_p,
        num_ctx=settings.llm_num_ctx,
        prefs=prefs,
    )

    async def embed(texts: list[str]) -> list[list[float]]:
        return await gateway.embed(texts, model=settings.memory_embed_model)

    memory = Memory(ConversationStore(engine), SemanticRecall(qdrant, embed))
    mcp_host = McpHost(settings.module_mcp_urls)
    agent = Agent(
        gateway=gateway,
        mcp=mcp_host,
        memory=memory,
        max_steps=settings.agent_max_steps,
        default_tenant=settings.default_tenant_id,
    )
    registry = ModuleRegistry(
        settings.module_base_urls,
        mcp=mcp_host,
        secrets=secrets,
        tenant=settings.default_tenant_id,
    )
    oauth = OAuthService(
        secrets,
        redirect_base_url=settings.oauth_redirect_base_url,
        state_secret=settings.oauth_state_secret,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await bus.connect()
        try:
            await prefs.init()
        except Exception as exc:
            log.error("llm prefs init failed; hide/default prefs disabled", error=str(exc))
        try:
            await memory.init()
        except Exception as exc:  # core stays up; cross-chat memory just degrades
            log.error("memory init failed; cross-chat memory disabled", error=str(exc))
        log.info("core runtime ready", tenant=settings.default_tenant_id)
        try:
            yield
        finally:
            await bus.close()
            await engine.dispose()
            await qdrant.close()

    app = FastAPI(title="epicurus core", lifespan=lifespan)
    add_ops_routes(app, service_name=SERVICE_NAME, version=_service_version())
    app.include_router(create_platform_router(settings, gateway))
    app.include_router(
        create_llm_router(gateway, prefs=prefs, default_tenant=settings.default_tenant_id)
    )
    app.include_router(create_power_router(gateway, power))
    app.include_router(create_agent_router(agent, memory, settings.default_tenant_id))
    app.include_router(create_modules_router(registry))
    app.include_router(create_oauth_router(oauth, default_tenant=settings.default_tenant_id))

    @app.exception_handler(GatewayPausedError)
    async def _on_paused(_request: Request, exc: GatewayPausedError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return app


app = create_app()
