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

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core import EventBus, SecretStore, add_ops_routes, configure_logging, get_logger
from epicurus_core_app.agent.agent import Agent
from epicurus_core_app.agent.attachment_sink import AttachmentSink
from epicurus_core_app.agent.attachments import AttachmentExpander
from epicurus_core_app.agent.builtins import NOW_SPEC, make_now_handler
from epicurus_core_app.agent.mcp_host import McpHost
from epicurus_core_app.agent.routes import create_agent_router
from epicurus_core_app.docker_control import DockerController
from epicurus_core_app.llm.catalog import ModelCatalog
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.power import GatewayPausedError, PowerController
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.routes import create_llm_router, create_power_router
from epicurus_core_app.log_stream import LogBuffer
from epicurus_core_app.log_stream_routes import create_log_stream_router
from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.memory.recall import SemanticRecall
from epicurus_core_app.memory.store import AttachmentStore, ConversationStore
from epicurus_core_app.module_prefs import ModulePrefsStore
from epicurus_core_app.modules import ModuleRegistry, create_modules_router
from epicurus_core_app.oauth.routes import create_oauth_router
from epicurus_core_app.oauth.service import OAuthService
from epicurus_core_app.platform_api import create_platform_router
from epicurus_core_app.readiness import ReadinessProbe, create_readiness_router
from epicurus_core_app.settings import CoreAppSettings
from epicurus_core_app.system_info import create_system_router
from epicurus_core_app.timezone_prefs import TimezonePrefsStore
from epicurus_core_app.timezone_routes import create_timezone_router

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
    log_buffer = LogBuffer()
    configure_logging(settings, extra_processors=[log_buffer.processor])
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
        default_embed_model=settings.memory_embed_model,
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

    catalog = ModelCatalog(
        source_url=settings.llm_catalog_url,
        refresh_seconds=settings.llm_catalog_refresh_seconds,
        max_models=settings.llm_catalog_max_models,
        enabled=settings.llm_catalog_enabled,
    )

    async def embed(texts: list[str]) -> list[list[float]]:
        # No explicit model → the gateway resolves the operator's Embedding-model pref
        # (effective_embed_default), falling back to settings.memory_embed_model. This is
        # what makes the UI "Embedding model" choice actually drive memory recall.
        return await gateway.embed(texts)

    memory = Memory(ConversationStore(engine), SemanticRecall(qdrant, embed))
    attachment_store = AttachmentStore(engine)
    attachment_sink = (
        AttachmentSink(settings.attachment_sink_url)
        if settings.attachment_sink_url.strip()
        else None
    )
    module_prefs = ModulePrefsStore(engine)
    timezone_prefs = TimezonePrefsStore(engine, default=settings.default_timezone)
    mcp_host = McpHost(settings.module_mcp_urls)
    registry = ModuleRegistry(
        settings.module_base_urls,
        mcp=mcp_host,
        secrets=secrets,
        tenant=settings.default_tenant_id,
        prefs=module_prefs,
        # Privileged, tightly-scoped Docker access for confirmed module removal (#127,
        # ADR-0028); None when the socket isn't mounted, in which case removal returns 503.
        docker=DockerController.from_env(),
    )
    # Agent tool discovery scans only enabled modules (#126); wired after the registry
    # exists (the host needs the registry and the registry needs the host).
    mcp_host.set_url_provider(registry.enabled_mcp_urls)
    # Per-tool disable filter (#213): tools the operator has individually turned off are
    # skipped in discover even when their module's URL is included.
    mcp_host.set_tool_filter(registry.disabled_tools_set)

    # Core `now` built-in tool (ADR-0039): reports the operator's configured timezone, and
    # best-effort the connected calendar's timezone when it differs. Registered after the
    # registry exists (the calendar lookup proxies the calendar module's /status).
    async def _calendar_timezone() -> str | None:
        try:
            status = await registry.get_status("calendar")
        except Exception:  # calendar absent / unreachable / not connected — never break `now`
            return None
        tz = status.get("google_timezone")
        return tz if isinstance(tz, str) else None

    mcp_host.register_builtin(
        "now",
        NOW_SPEC,
        make_now_handler(
            lambda: timezone_prefs.get_timezone(settings.default_tenant_id),
            _calendar_timezone,
        ),
    )
    agent = Agent(
        gateway=gateway,
        mcp=mcp_host,
        memory=memory,
        max_steps=settings.agent_max_steps,
        default_tenant=settings.default_tenant_id,
        attachments=AttachmentExpander(store=attachment_store, memory=memory, registry=registry),
        # Resolve the loop bound per turn from the stored pref (else the env default), so the
        # operator's UI choice takes effect without a restart (#297).
        prefs=prefs,
    )
    oauth = OAuthService(
        secrets,
        redirect_base_url=settings.oauth_redirect_base_url,
        state_secret=settings.oauth_state_secret,
    )
    readiness = ReadinessProbe(
        power=power,
        gateway=gateway,
        registry=registry,
        default_tenant=settings.default_tenant_id,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await bus.connect()
        try:
            await prefs.init()
        except Exception as exc:
            log.error("llm prefs init failed; hide/default prefs disabled", error=str(exc))
        try:
            await module_prefs.init()
        except Exception as exc:
            log.error("module prefs init failed; enable/disable unavailable", error=str(exc))
        try:
            await timezone_prefs.init()
        except Exception as exc:
            log.error("timezone prefs init failed; timezone setting disabled", error=str(exc))
        try:
            await registry.reconcile_tombstones()
        except Exception as exc:  # best-effort — a Docker hiccup must never block startup
            log.error("tombstone reconcile failed", error=str(exc))
        try:
            await memory.init()
        except Exception as exc:  # core stays up; cross-chat memory just degrades
            log.error("memory init failed; cross-chat memory disabled", error=str(exc))
        # Parse the model catalog from upstream on a loop (#269). Fire-and-forget so
        # startup never blocks on the network; the task self-heals on transient failures.
        catalog_task = asyncio.create_task(catalog.run_periodic())
        log.info("core runtime ready", tenant=settings.default_tenant_id)
        try:
            yield
        finally:
            catalog_task.cancel()
            with suppress(asyncio.CancelledError):
                await catalog_task
            await bus.close()
            await engine.dispose()
            await qdrant.close()

    app = FastAPI(title="epicurus core", lifespan=lifespan)
    add_ops_routes(app, service_name=SERVICE_NAME, version=_service_version())
    app.include_router(
        create_platform_router(
            settings,
            gateway,
            prefs=prefs,
            default_tenant=settings.default_tenant_id,
        )
    )
    app.include_router(
        create_llm_router(
            gateway, prefs=prefs, default_tenant=settings.default_tenant_id, catalog=catalog
        )
    )
    app.include_router(
        create_timezone_router(timezone_prefs, default_tenant=settings.default_tenant_id)
    )
    app.include_router(create_power_router(gateway, power))
    app.include_router(
        create_agent_router(
            agent,
            memory,
            settings.default_tenant_id,
            attachment_store,
            sink=attachment_sink,
            probe=readiness,
            max_upload_bytes=settings.attachment_max_bytes,
            allowed_upload_types=settings.attachment_allowed_type_list,
        )
    )
    app.include_router(create_readiness_router(readiness))
    app.include_router(create_system_router(gateway))
    app.include_router(create_modules_router(registry))
    app.include_router(
        create_oauth_router(
            oauth,
            default_tenant=settings.default_tenant_id,
            # Connecting/disconnecting a provider auto-seeds/clears the collection
            # selection of the modules that use it (#209).
            collections=registry,
        )
    )
    app.include_router(create_log_stream_router(log_buffer))

    @app.exception_handler(GatewayPausedError)
    async def _on_paused(_request: Request, exc: GatewayPausedError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return app


app = create_app()
