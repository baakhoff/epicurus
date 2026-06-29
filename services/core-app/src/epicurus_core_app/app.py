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

from epicurus_core import (
    EventBus,
    SecretStore,
    add_ops_routes,
    build_file_store,
    configure_logging,
    get_logger,
)
from epicurus_core_app.agent.agent import Agent
from epicurus_core_app.agent.attachment_sink import AttachmentSink
from epicurus_core_app.agent.attachments import AttachmentExpander
from epicurus_core_app.agent.builtins import (
    ASK_USER_SPEC,
    ASK_USER_TOOL,
    NOW_SPEC,
    REMEMBER_SPEC,
    make_ask_user_handler,
    make_now_handler,
    make_remember_handler,
)
from epicurus_core_app.agent.live_runs import LiveRunRegistry
from epicurus_core_app.agent.mcp_host import McpHost
from epicurus_core_app.agent.routes import create_agent_router
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.docker_control import DockerController
from epicurus_core_app.files_routes import create_files_router
from epicurus_core_app.llm.catalog import ModelCatalog
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.model_settings import ModelSettingsStore
from epicurus_core_app.llm.ollama_runtime import OllamaRuntime
from epicurus_core_app.llm.power import GatewayPausedError, PowerController
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.routes import create_llm_router, create_power_router
from epicurus_core_app.llm.variants import VariantLookup
from epicurus_core_app.log_stream import LogBuffer
from epicurus_core_app.log_stream_routes import create_log_stream_router
from epicurus_core_app.memory.extraction import ExtractionRunner, FactExtractor
from epicurus_core_app.memory.extraction_queue import ExtractionQueue
from epicurus_core_app.memory.facts import UserFactStore
from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.memory.store import AttachmentStore, ConversationStore
from epicurus_core_app.module_prefs import ModulePrefsStore
from epicurus_core_app.modules import (
    ModuleRegistry,
    create_modules_router,
    create_suggestions_router,
)
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
    model_settings = ModelSettingsStore(engine)
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
        model_settings=model_settings,
    )

    catalog = ModelCatalog(
        source_url=settings.llm_catalog_url,
        refresh_seconds=settings.llm_catalog_refresh_seconds,
        max_models=settings.llm_catalog_max_models,
        enabled=settings.llm_catalog_enabled,
    )
    # On-demand quant-variant lookup from each model's public library tags page (#330).
    variant_lookup = VariantLookup(library_url=settings.llm_catalog_url)

    async def embed(texts: list[str]) -> list[list[float]]:
        # No explicit model → the gateway resolves the operator's Embedding-model pref
        # (effective_embed_default), falling back to settings.memory_embed_model. This is
        # what makes the UI "Embedding model" choice actually drive memory recall.
        return await gateway.embed(texts)

    # Cross-chat memory is a corpus of durable *facts* about the user (ADR-0045), written by
    # the agent's `remember` tool and by background extraction — not a dump of raw messages.
    facts = UserFactStore(qdrant, embed)
    memory = Memory(ConversationStore(engine), facts)
    # Deferred fact extraction (ADR-0051): finished exchanges queue here, and a nightly runner
    # distils them off-hours so extraction never competes with a live turn for the GPU. The
    # extractor may target a small dedicated model to keep the nightly pass cheap.
    extraction_queue = ExtractionQueue(engine)
    extractor = FactExtractor(gateway, facts, model=settings.memory_extraction_model or None)
    attachment_store = AttachmentStore(engine)
    attachment_sink = (
        AttachmentSink(settings.attachment_sink_url)
        if settings.attachment_sink_url.strip()
        else None
    )
    module_prefs = ModulePrefsStore(engine)
    timezone_prefs = TimezonePrefsStore(engine, default=settings.default_timezone)
    # Durable state behind ask_user pause/resume (ADR-0053): a paused turn lives here until
    # the operator answers (or it expires).
    suspended_runs = SuspendedRunStore(engine, ttl_hours=settings.ask_user_ttl_hours)
    # In-flight turns, decoupled from the request that started them (#376): a turn runs in a
    # detached task that buffers its events, so a client disconnect (PWA backgrounded, refresh)
    # no longer aborts it — the answer still persists and a reconnecting client re-attaches.
    live_runs = LiveRunRegistry(grace_seconds=settings.live_run_grace_seconds)
    mcp_host = McpHost(settings.module_mcp_urls)
    # One tightly-scoped Docker handle (#127, ADR-0028): module removal for the registry, plus a
    # restart-only path for Ollama's KV-cache apply (#307). None when the socket isn't mounted —
    # removal returns 503, and a KV-cache change saves but isn't applied (manual restart).
    docker = DockerController.from_env()
    registry = ModuleRegistry(
        settings.module_base_urls,
        mcp=mcp_host,
        secrets=secrets,
        tenant=settings.default_tenant_id,
        prefs=module_prefs,
        docker=docker,
    )
    ollama_runtime = OllamaRuntime(
        docker,
        env_path=settings.ollama_runtime_env_path,
        service=settings.ollama_service_name,
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
    # Core `remember` built-in tool (ADR-0045): the agent's explicit path for saving a durable
    # fact about the user to long-term memory; background extraction covers the implicit path.
    mcp_host.register_builtin("remember", REMEMBER_SPEC, make_remember_handler(memory))
    # Core `ask_user` built-in tool (ADR-0053): lets the model pause the turn to ask a
    # clarifying question. The agent loop intercepts the call to suspend; this handler is a
    # safety net (the spec reaches the model via the same discovery path as now/remember).
    mcp_host.register_builtin(ASK_USER_TOOL, ASK_USER_SPEC, make_ask_user_handler())
    agent = Agent(
        gateway=gateway,
        mcp=mcp_host,
        memory=memory,
        max_steps=settings.agent_max_steps,
        default_tenant=settings.default_tenant_id,
        attachments=AttachmentExpander(store=attachment_store, memory=memory, registry=registry),
        extractor=extractor,
        # Deferred extraction (ADR-0051): in "nightly" mode the agent enqueues each exchange here
        # instead of distilling it inline; "immediate" mode still fires the extractor per turn.
        queue=extraction_queue,
        defer_extraction=settings.defer_extraction,
        # Time-box the one inline memory step so a cold embedder can't stall the first token.
        recall_timeout_s=settings.memory_recall_timeout_s,
        # Resolve the loop bound per turn from the stored pref (else the env default), so the
        # operator's UI choice takes effect without a restart (#297).
        prefs=prefs,
        suspended=suspended_runs,
    )
    # Nightly fact-extraction runner (ADR-0051): drains the queue once a day, in the operator's
    # timezone, serially — so extraction happens off-hours, never against a live turn.
    extraction_runner = ExtractionRunner(
        extraction_queue,
        extractor,
        power,
        timezone=lambda: timezone_prefs.get_timezone(settings.default_tenant_id),
        hour=settings.memory_extraction_hour,
        batch_limit=settings.memory_extraction_batch_limit,
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
    # Core-owned file space (ADR-0052): the swappable backend behind /platform/v1/files that
    # modules consume instead of mounting the shared volume. Default backend is the local FS.
    file_store = build_file_store(
        backend=settings.files_backend,
        root=settings.files_root,
        s3_url=settings.files_s3_url,
        s3_access_key=settings.files_s3_access_key,
        s3_secret_key=settings.files_s3_secret_key,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await bus.connect()
        try:
            await prefs.init()
        except Exception as exc:
            log.error("llm prefs init failed; hide/default prefs disabled", error=str(exc))
        try:
            await model_settings.init()
        except Exception as exc:
            log.error("model settings init failed; per-model tuning disabled", error=str(exc))
        try:
            await module_prefs.init()
        except Exception as exc:
            log.error("module prefs init failed; enable/disable unavailable", error=str(exc))
        try:
            await timezone_prefs.init()
        except Exception as exc:
            log.error("timezone prefs init failed; timezone setting disabled", error=str(exc))
        try:
            await suspended_runs.init()
        except Exception as exc:
            log.error("suspended-run store init failed; ask_user pause/resume off", error=str(exc))
        try:
            await registry.reconcile_tombstones()
        except Exception as exc:  # best-effort — a Docker hiccup must never block startup
            log.error("tombstone reconcile failed", error=str(exc))
        try:
            await memory.init()
        except Exception as exc:  # core stays up; cross-chat memory just degrades
            log.error("memory init failed; cross-chat memory disabled", error=str(exc))
        try:
            await extraction_queue.init()
        except Exception as exc:  # queue down → deferred extraction degrades; chat is unaffected
            log.error("extraction queue init failed; nightly extraction off", error=str(exc))
        # Provision the tenant's file-space root (core-owned provisioning, ADR-0052). Until the
        # shared volume is mounted into the core (a follow-up phase), the local root won't exist
        # yet — skip cleanly rather than logging an error on every boot.
        if settings.files_backend != "local" or settings.files_root.exists():
            try:
                await file_store.ensure_tenant_root(tenant=settings.default_tenant_id)
            except Exception as exc:  # never block startup on a provisioning hiccup
                log.warning("file-space provisioning failed", error=str(exc))
        # Parse the model catalog from upstream on a loop (#269). Fire-and-forget so
        # startup never blocks on the network; the task self-heals on transient failures.
        catalog_task = asyncio.create_task(catalog.run_periodic())
        # Distil queued exchanges into durable user facts on a nightly schedule (ADR-0051).
        # Fire-and-forget: it sleeps until the operator's configured hour, then drains serially.
        extraction_task = asyncio.create_task(extraction_runner.run_periodic())
        # Evict finished in-flight runs past their grace window (#376), even with no new traffic.
        live_run_reaper = asyncio.create_task(live_runs.reap_periodically())
        log.info("core runtime ready", tenant=settings.default_tenant_id)
        try:
            yield
        finally:
            catalog_task.cancel()
            extraction_task.cancel()
            live_run_reaper.cancel()
            with suppress(asyncio.CancelledError):
                await catalog_task
            with suppress(asyncio.CancelledError):
                await extraction_task
            with suppress(asyncio.CancelledError):
                await live_run_reaper
            # Let in-flight turns finish (and persist) before the engine closes under them (#376).
            await live_runs.drain()
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
    app.include_router(create_files_router(file_store, default_tenant=settings.default_tenant_id))
    app.include_router(
        create_llm_router(
            gateway,
            prefs=prefs,
            default_tenant=settings.default_tenant_id,
            catalog=catalog,
            variants=variant_lookup,
            model_settings=model_settings,
            ollama_runtime=ollama_runtime,
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
            suspended=suspended_runs,
            live_runs=live_runs,
            max_upload_bytes=settings.attachment_max_bytes,
            allowed_upload_types=settings.attachment_allowed_type_list,
        )
    )
    app.include_router(create_readiness_router(readiness))
    app.include_router(create_system_router(gateway))
    app.include_router(create_modules_router(registry))
    app.include_router(create_suggestions_router(registry))
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
