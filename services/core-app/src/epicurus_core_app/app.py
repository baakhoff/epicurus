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
    setup_tracing,
)
from epicurus_core_app.agent.agent import Agent
from epicurus_core_app.agent.attachment_sink import AttachmentSink
from epicurus_core_app.agent.attachments import AttachmentExpander
from epicurus_core_app.agent.builtins import (
    ASK_USER_SPEC,
    ASK_USER_TOOL,
    MEMORY_SEARCH_SPEC,
    MEMORY_SEARCH_TOOL,
    NOW_SPEC,
    REMEMBER_SPEC,
    make_ask_user_handler,
    make_memory_search_handler,
    make_now_handler,
    make_remember_handler,
)
from epicurus_core_app.agent.instructions import AgentInstructionsStore
from epicurus_core_app.agent.instructions_routes import create_instructions_router
from epicurus_core_app.agent.live_runs import LiveRunRegistry
from epicurus_core_app.agent.mcp_host import McpHost
from epicurus_core_app.agent.pending_drafts import PendingDraftStore
from epicurus_core_app.agent.playbook_review import CoreReviewPage, PlaybookProposalStore
from epicurus_core_app.agent.playbooks import PlaybookStore
from epicurus_core_app.agent.reflection import PlaybookReflector, ReflectionStateStore
from epicurus_core_app.agent.routes import create_agent_router
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.docker_control import DockerController
from epicurus_core_app.file_index import FileIndex
from epicurus_core_app.file_scan import scan as scan_file_space
from epicurus_core_app.file_watch import FileWatcher
from epicurus_core_app.files_routes import create_files_router
from epicurus_core_app.llm.catalog import ModelCatalog
from epicurus_core_app.llm.gateway import LlmGateway
from epicurus_core_app.llm.model_settings import ModelSettingsStore
from epicurus_core_app.llm.ollama_runtime import OllamaRuntime
from epicurus_core_app.llm.power import GatewayPausedError, PowerController
from epicurus_core_app.llm.prefs import LlmPrefsStore
from epicurus_core_app.llm.routes import create_llm_router, create_power_router
from epicurus_core_app.llm.saved_models import SavedHostedModelStore
from epicurus_core_app.llm.variants import VariantLookup
from epicurus_core_app.log_stream import LogBuffer
from epicurus_core_app.log_stream_routes import create_log_stream_router
from epicurus_core_app.maintenance import (
    MaintenanceOrchestrator,
    extraction_drain_job,
    facts_reembed_job,
    module_reindex_job,
    playbook_reflection_job,
    profile_synthesis_job,
)
from epicurus_core_app.maintenance_routes import create_maintenance_router
from epicurus_core_app.maintenance_schedule_prefs import MaintenanceScheduleStore
from epicurus_core_app.memory.extraction import ExtractionRunner, FactExtractor
from epicurus_core_app.memory.extraction_queue import ExtractionQueue
from epicurus_core_app.memory.facts import UserFactStore
from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.memory.profile import ProfileSynthesizer, StandingProfileStore
from epicurus_core_app.memory.store import AttachmentStore, ConversationStore
from epicurus_core_app.messaging import (
    BridgeAdmin,
    InboundConsumer,
    RegistryBridgeClient,
    create_messaging_router,
)
from epicurus_core_app.module_prefs import ModulePrefsStore
from epicurus_core_app.modules import (
    ModuleRegistry,
    create_calendar_feed_router,
    create_modules_router,
    create_suggestions_router,
)
from epicurus_core_app.oauth.routes import create_oauth_router
from epicurus_core_app.oauth.service import OAuthService
from epicurus_core_app.object_backend import StorageObjectBackend
from epicurus_core_app.page_order_prefs import PageOrderStore
from epicurus_core_app.page_order_routes import create_page_order_router
from epicurus_core_app.platform_api import create_platform_router
from epicurus_core_app.readiness import ReadinessProbe, create_readiness_router
from epicurus_core_app.scheduled_turns import ScheduledTurnScheduler, ScheduledTurnStore
from epicurus_core_app.scheduled_turns_routes import create_scheduled_turns_router
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
    # The operator's saved hosted-model ids (#496) — a durable, tenant-scoped home for the
    # hosted model strings entered in the picker, so they survive restarts / a PWA reinstall and
    # follow the tenant across devices (unlike the browser's per-origin recentModels cache).
    saved_models = SavedHostedModelStore(engine)
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
        timeout=settings.llm_timeout,
        temperature=settings.llm_temperature,
        top_p=settings.llm_top_p,
        num_ctx=settings.llm_num_ctx,
        prefs=prefs,
        model_settings=model_settings,
    )

    # On-demand quant-variant lookup from each model's public library tags page (#330).
    # Its per-family cache lives for one catalog refresh interval, so the catalog's GB size
    # fill (#571) and the UI's variant lookups share a single upstream request per family.
    variant_lookup = VariantLookup(
        library_url=settings.llm_catalog_url,
        cache_ttl_seconds=settings.llm_catalog_refresh_seconds,
    )
    catalog = ModelCatalog(
        source_url=settings.llm_catalog_url,
        refresh_seconds=settings.llm_catalog_refresh_seconds,
        max_models=settings.llm_catalog_max_models,
        enabled=settings.llm_catalog_enabled,
        tag_source=variant_lookup.family_tags,
        size_fill_seconds=settings.llm_catalog_size_fill_seconds,
    )

    async def embed(texts: list[str]) -> list[list[float]]:
        # No explicit model → the gateway resolves the operator's Embedding-model pref
        # (effective_embed_default), falling back to settings.memory_embed_model. This is
        # what makes the UI "Embedding model" choice actually drive memory recall.
        return await gateway.embed(texts)

    # Cross-chat memory is a corpus of durable *facts* about the user (ADR-0045), written by
    # the agent's `remember` tool and by background extraction — not a dump of raw messages.
    facts = UserFactStore(qdrant, embed)
    conversation_store = ConversationStore(engine)
    memory = Memory(conversation_store, facts)
    # Deferred fact extraction (ADR-0051): finished exchanges queue here, and a nightly runner
    # distils them off-hours so extraction never competes with a live turn for the GPU. The
    # extractor may target a small dedicated model to keep the nightly pass cheap.
    extraction_queue = ExtractionQueue(engine)
    extractor = FactExtractor(gateway, facts, model=settings.memory_extraction_model or None)
    # Standing user profile (ADR-0094): a compact per-tenant picture of the user, synthesized from
    # the fact store on the nightly maintenance batch and injected STATICALLY in _assemble — no
    # turn-time embed — so the common-case recall cost leaves the response path (the ADR-0051 trade,
    # now for the profile). Best-effort: no profile → exactly today's behavior.
    profile_store = StandingProfileStore(engine, max_versions=settings.memory_profile_max_versions)
    profile_synthesizer = ProfileSynthesizer(
        gateway,
        facts,
        profile_store,
        tenants=conversation_store.distinct_tenants,
        model=settings.memory_profile_model or None,
    )
    attachment_store = AttachmentStore(engine)
    attachment_sink = (
        AttachmentSink(settings.attachment_sink_url)
        if settings.attachment_sink_url.strip()
        else None
    )
    module_prefs = ModulePrefsStore(engine)
    timezone_prefs = TimezonePrefsStore(engine, default=settings.default_timezone)
    # The maintenance orchestrator's schedule (#621): one row per tenant, falling back to the env
    # defaults until an operator sets their own via PUT. Read by both the orchestrator's poll loop
    # (below) and the maintenance routes; written only by the routes.
    maintenance_schedule_prefs = MaintenanceScheduleStore(
        engine,
        default_enabled=settings.maintenance_schedule_enabled,
        default_hour=settings.maintenance_hour,
    )
    # The operator's drag-and-drop left-nav page order (#543): one row per tenant, syncing
    # across devices. Resolved/merged client-side (ADR-0018) — this store is opaque storage.
    page_order_prefs = PageOrderStore(engine)
    # Recurring prompts that run unattended and deliver into a session (ADR-0092): the
    # tenant-scoped row store; the scheduler poll loop is built below, once `agent` exists.
    scheduled_turns = ScheduledTurnStore(engine)
    # Named playbooks (ADR-0093 §3): independent, enable-able blocks of guidance beside the base
    # prompt, versioned ADR-0046-style. Composed into the prompt by the instructions store below.
    agent_playbooks = PlaybookStore(engine)
    # The agent's editable base system prompt (#497, ADR-0083): one row per tenant, NULL = the
    # shipped default. Resolved per turn in ``Agent._assemble``, edited in web Settings. Given the
    # playbook store it returns base + every enabled playbook as one string (ADR-0093 §4), so the
    # assembly call site never learned playbooks exist.
    agent_instructions = AgentInstructionsStore(engine, playbooks=agent_playbooks)
    # The staged, not-yet-approved edits to the two stores above (ADR-0093 §2) plus the durable
    # resolved-decision trail (ADR-0090) the reflection pass reads back as negative context.
    playbook_proposals = PlaybookProposalStore(engine)
    # The nightly pass that *proposes* those edits (ADR-0093 §1): one gateway call per active
    # tenant over the sessions it saw since its last run, metered under that tenant (§5). It is
    # handed the proposal sink and read-only playbook + base-instructions lookups — never the
    # stores that own the documents — so it is structurally incapable of applying anything itself.
    playbook_reflection_state = ReflectionStateStore(engine)
    playbook_reflector = PlaybookReflector(
        gateway,
        conversation_store,
        playbook_proposals,
        agent_playbooks,
        agent_instructions,
        playbook_reflection_state,
        tenants=conversation_store.distinct_tenants,
        model=settings.playbook_reflection_model or None,
    )
    # The reserved ``core`` pseudo-module: the core's own ``review`` page, answered in-process by
    # the registry rather than probed over HTTP (ADR-0093 §2). Approving here is the *only* path
    # that writes agent instructions/playbooks on the agent's behalf — nothing self-applies.
    core_review = CoreReviewPage(
        store=playbook_proposals,
        instructions=agent_instructions,
        playbooks=agent_playbooks,
        tenant=settings.default_tenant_id,
        version=_service_version(),
    )
    # Durable state behind ask_user pause/resume (ADR-0053): a paused turn lives here until
    # the operator answers (or it expires).
    suspended_runs = SuspendedRunStore(engine, ttl_hours=settings.ask_user_ttl_hours)
    # Durable state behind draft-first send Confirm/Decline (ADR-0085, #563): a turn paused on a
    # composed outbound draft lives here until the operator confirms/declines (or it expires).
    pending_drafts = PendingDraftStore(engine, ttl_hours=settings.draft_review_ttl_hours)
    # In-flight turns, decoupled from the request that started them (#376): a turn runs in a
    # detached task that buffers its events, so a client disconnect (PWA backgrounded, refresh)
    # no longer aborts it — the answer still persists and a reconnecting client re-attaches.
    live_runs = LiveRunRegistry(grace_seconds=settings.live_run_grace_seconds)
    mcp_host = McpHost(settings.module_mcp_urls)
    # One tightly-scoped Docker handle (#127, ADR-0028): module removal for the registry, plus a
    # restart-only path for Ollama's KV-cache apply (#307). The socket is an explicit opt-in
    # (ADR-0099) — unavailable by default, which defers container teardown on removal to the
    # next restart and leaves a KV-cache change unapplied until a manual restart; it never
    # disables removal itself (ADR-0056/#382) or blocks startup.
    docker_availability = DockerController.from_env()
    docker = docker_availability.controller
    registry = ModuleRegistry(
        settings.module_base_urls,
        mcp=mcp_host,
        secrets=secrets,
        tenant=settings.default_tenant_id,
        prefs=module_prefs,
        docker=docker,
        docker_unavailable_reason=docker_availability.reason,
        core=core_review,
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
    # Core `memory_search` built-in tool (ADR-0089): deliberate recall over the fact store and
    # past conversations — the agent-initiated complement to the ambient recall `_assemble`
    # injects each turn. Tenant-scoped (built-ins receive the tenant), best-effort like recall.
    mcp_host.register_builtin(
        MEMORY_SEARCH_TOOL, MEMORY_SEARCH_SPEC, make_memory_search_handler(memory)
    )
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
        pending_drafts=pending_drafts,
        # The editable base system prompt (#497), resolved per turn and injected first — so both
        # chat and the headless bridge consumer below run with the same instructions.
        instructions=agent_instructions,
        # The standing profile (#527), injected statically after the base prompt with no embed —
        # so both chat and the headless bridge carry the same durable picture of the user.
        profile=profile_store,
        # The registry's read-only view of which tools write a document (#541, ADR-0100): the
        # annotation lives only in module manifests, which the registry alone fetches. Passing
        # the bound lookup rather than the registry keeps the loop unaware it exists.
        documents=registry.document_tool,
    )
    # Inbound messaging consumer (ADR-0058) — the first inbound NATS subscriber in core. It
    # turns a bridge message (``messaging.inbound``) into a headless agent turn and routes the
    # reply back out (``messaging.outbound``); the ``messaging`` module carries both ends. Wired
    # here, subscribed in the lifespan once the bus is up (gated by MESSAGING_INBOUND_ENABLED).
    inbound_messaging = InboundConsumer(
        bus=bus,
        agent=agent,
        power=power,
        default_tenant=settings.default_tenant_id,
        model=settings.messaging_model or None,
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
    # Scheduled-turns poll loop (ADR-0092): the same headless-turn shape InboundConsumer uses
    # for a bridge message (no HTTP caller, an explicit tenant_id + session_id) — reused here
    # for a due recurring prompt instead of an inbound message.
    scheduled_turn_scheduler = ScheduledTurnScheduler(
        scheduled_turns,
        agent,
        power,
        timezone=lambda: timezone_prefs.get_timezone(settings.default_tenant_id),
        poll_interval_s=settings.scheduled_turns_poll_interval_s,
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
    # The core-owned Files UI (ADR-0063, Phase 2): a file index catalogues the FileStore tree —
    # walked at startup, watched for changes — and powers the Files page + search; the object
    # backend folds in the storage module's uploads/agent objects so the core serves one unified
    # Files view. Only the local backend has a real tree to watch (S3 has no inotify surface).
    file_index = FileIndex(engine)
    file_objects = StorageObjectBackend(registry)
    file_scan_lock = asyncio.Lock()

    async def _rescan_files() -> int:
        # Serialise the startup walk and every watch-triggered rescan against each other (the
        # scan holds no internal lock), so a change mid-startup waits rather than double-walks.
        async with file_scan_lock:
            return await scan_file_space(file_store, file_index, tenant=settings.default_tenant_id)

    file_watcher = (
        FileWatcher(
            settings.files_root / settings.default_tenant_id,
            _rescan_files,
            debounce_ms=settings.files_watch_debounce_ms,
        )
        if settings.files_backend == "local" and settings.files_watch
        else None
    )
    # Maintenance orchestrator (ADR-0060): one coordinated batch over the background jobs — the
    # deferred fact-extraction drain (light, nightly-eligible) and the module re-index fan-out
    # (heavy, manual-only). The manual "run everything" trigger is always available; the nightly
    # schedule is opt-in (MAINTENANCE_SCHEDULE_ENABLED) to avoid redundant work with the per-runner
    # schedules. New job types register by being added to this list.
    maintenance = MaintenanceOrchestrator(
        [
            extraction_drain_job(extraction_runner.drain_once),
            profile_synthesis_job(profile_synthesizer.run),
            playbook_reflection_job(playbook_reflector.run),
            module_reindex_job(registry.reembed),
            facts_reembed_job(lambda: facts.reembed_all(tenant=settings.default_tenant_id)),
        ],
        bus=bus,
        default_tenant=settings.default_tenant_id,
        timezone=lambda: timezone_prefs.get_timezone(settings.default_tenant_id),
        schedule=lambda: maintenance_schedule_prefs.get(settings.default_tenant_id),
        poll_interval_s=settings.maintenance_poll_interval_s,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await bus.connect()
        try:
            await prefs.init()
        except Exception as exc:
            log.error("llm prefs init failed; hide/default prefs disabled", error=str(exc))
        try:
            await saved_models.init()
        except Exception as exc:
            log.error("saved-models init failed; hosted-model list disabled", error=str(exc))
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
            await maintenance_schedule_prefs.init()
        except Exception as exc:
            log.error(
                "maintenance schedule prefs init failed; schedule falls back to env config",
                error=str(exc),
            )
        try:
            await page_order_prefs.init()
        except Exception as exc:
            log.error("page-order prefs init failed; nav reorder disabled", error=str(exc))
        try:
            await agent_instructions.init()
        except Exception as exc:
            log.error("agent instructions init failed; using the default prompt", error=str(exc))
        try:
            await agent_playbooks.init()
        except Exception as exc:
            # The composed prompt degrades to the base instructions alone (ADR-0093 §4's
            # best-effort read), so a failure here costs playbooks, never every turn.
            log.error("agent playbooks init failed; playbooks disabled", error=str(exc))
        try:
            await playbook_proposals.init()
        except Exception as exc:
            log.error(
                "playbook proposal store init failed; the core review page is empty",
                error=str(exc),
            )
        try:
            await playbook_reflection_state.init()
        except Exception as exc:
            # Without its watermark the nightly pass can't tell new sessions from old, so it
            # errors as a contained job result rather than re-proposing the whole history.
            log.error(
                "playbook reflection state init failed; nightly reflection off", error=str(exc)
            )
        try:
            await suspended_runs.init()
        except Exception as exc:
            log.error("suspended-run store init failed; ask_user pause/resume off", error=str(exc))
        try:
            await pending_drafts.init()
        except Exception as exc:
            log.error("pending-draft store init failed; draft-first send off", error=str(exc))
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
        try:
            await scheduled_turns.init()
        except Exception as exc:  # store down → scheduled turns degrade; chat is unaffected
            log.error("scheduled-turns init failed; scheduled turns disabled", error=str(exc))
        try:
            await profile_store.init()
        except Exception as exc:  # profile down → static injection degrades; recall still runs
            log.error("standing-profile store init failed; profile injection off", error=str(exc))
        # Provision the tenant's file-space root and index it (core-owned, ADR-0052/0061). The
        # core now mounts the shared volume (Phase 2), so the local root exists at boot: init the
        # file index, ensure the tenant root, then walk it in. Best-effort throughout — a DB or
        # scan hiccup degrades the Files page rather than blocking startup.
        try:
            await file_index.init()
        except Exception as exc:
            log.error("file index init failed; Files search disabled", error=str(exc))
        if settings.files_backend != "local" or settings.files_root.exists():
            try:
                await file_store.ensure_tenant_root(tenant=settings.default_tenant_id)
                await _rescan_files()
            except Exception as exc:  # never block startup on a provisioning/scan hiccup
                log.warning("file-space provisioning/scan failed", error=str(exc))
        # Parse the model catalog from upstream on a loop (#269). Fire-and-forget so
        # startup never blocks on the network; the task self-heals on transient failures.
        catalog_task = asyncio.create_task(catalog.run_periodic())
        # Backfill GB sizes from each family's tags page, rate-limited (#571). Returns
        # immediately when the catalog is disabled, so air-gapped builds never fetch.
        catalog_size_task = asyncio.create_task(catalog.run_size_fill())
        # Distil queued exchanges into durable user facts on a nightly schedule (ADR-0051).
        # Fire-and-forget: it sleeps until the operator's configured hour, then drains serially.
        extraction_task = asyncio.create_task(extraction_runner.run_periodic())
        # Scheduled turns (ADR-0092): a plain poll loop finding due rows and delivering each as
        # a headless agent turn. Fire-and-forget, same shape as the tasks around it.
        scheduled_turns_task = asyncio.create_task(scheduled_turn_scheduler.run_periodic())
        # Evict finished in-flight runs past their grace window (#376), even with no new traffic.
        live_run_reaper = asyncio.create_task(live_runs.reap_periodically())
        # Keep the file index live: re-walk on a debounced change (local backend only, ADR-0063).
        file_watch_task = (
            asyncio.create_task(file_watcher.run()) if file_watcher is not None else None
        )
        # Start consuming inbound bridge messages (ADR-0058). Best-effort: a NATS hiccup here
        # must not block startup — the rest of the core (web chat) stays up regardless.
        if settings.messaging_inbound_enabled:
            try:
                await inbound_messaging.start()
            except Exception as exc:
                log.error("inbound messaging consumer failed to start", error=str(exc))
        # Coordinated maintenance batch on an opt-in nightly schedule (ADR-0060) — a no-op task when
        # the schedule is disabled; the manual trigger stays available either way.
        maintenance_task = asyncio.create_task(maintenance.run_periodic())
        log.info("core runtime ready", tenant=settings.default_tenant_id)
        try:
            yield
        finally:
            # Stop accepting new inbound messages first, so no turn starts mid-teardown.
            await inbound_messaging.stop()
            catalog_task.cancel()
            catalog_size_task.cancel()
            extraction_task.cancel()
            scheduled_turns_task.cancel()
            live_run_reaper.cancel()
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await catalog_task
            with suppress(asyncio.CancelledError):
                await catalog_size_task
            with suppress(asyncio.CancelledError):
                await extraction_task
            with suppress(asyncio.CancelledError):
                await scheduled_turns_task
            with suppress(asyncio.CancelledError):
                await live_run_reaper
            with suppress(asyncio.CancelledError):
                await maintenance_task
            # The nightly-schedule loop above is separate from an in-flight batch it (or a manual
            # trigger) may have started — cancel that too so it isn't orphaned against infra about
            # to close (#561).
            await maintenance.shutdown()
            if file_watcher is not None and file_watch_task is not None:
                file_watcher.stop()
                file_watch_task.cancel()
                with suppress(asyncio.CancelledError):
                    await file_watch_task
            # Let in-flight turns finish (and persist) before the engine closes under them (#376).
            await live_runs.drain()
            await bus.close()
            await engine.dispose()
            await qdrant.close()

    app = FastAPI(title="epicurus core", lifespan=lifespan)
    add_ops_routes(app, service_name=SERVICE_NAME, version=_service_version())
    # Distributed tracing to Tempo (#57) — a no-op unless OTEL_TRACES_ENABLED is set.
    # Instruments every router below (agent loop, LLM gateway, platform API, files);
    # EventBus publish/handle spans link in over NATS for one trace across the stack.
    setup_tracing(app, settings, version=_service_version())
    app.include_router(
        create_platform_router(
            settings,
            gateway,
            prefs=prefs,
            default_tenant=settings.default_tenant_id,
        )
    )
    app.include_router(
        create_files_router(
            file_store,
            default_tenant=settings.default_tenant_id,
            index=file_index,
            objects=file_objects,
            # The Files-page upload door shares the chat-attachment caps (#175 → #479).
            max_upload_bytes=settings.attachment_max_bytes,
            allowed_upload_types=settings.attachment_allowed_type_list,
            locked_prefixes=frozenset(settings.module_hostnames),
        )
    )
    app.include_router(
        create_maintenance_router(
            maintenance,
            schedule_store=maintenance_schedule_prefs,
            timezone=lambda: timezone_prefs.get_timezone(settings.default_tenant_id),
            default_tenant=settings.default_tenant_id,
        )
    )
    app.include_router(
        create_llm_router(
            gateway,
            prefs=prefs,
            default_tenant=settings.default_tenant_id,
            catalog=catalog,
            variants=variant_lookup,
            model_settings=model_settings,
            ollama_runtime=ollama_runtime,
            saved_models=saved_models,
        )
    )
    app.include_router(
        create_timezone_router(timezone_prefs, default_tenant=settings.default_tenant_id)
    )
    app.include_router(
        create_page_order_router(page_order_prefs, default_tenant=settings.default_tenant_id)
    )
    app.include_router(
        create_instructions_router(agent_instructions, default_tenant=settings.default_tenant_id)
    )
    app.include_router(
        create_scheduled_turns_router(scheduled_turns, default_tenant=settings.default_tenant_id)
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
            pending_drafts=pending_drafts,
            # Draft-first send (ADR-0085, #563): on Confirm the route transmits the reviewed draft
            # via the owning module's ``POST /send`` — the one place the core sends outbound mail.
            send_draft=registry.send_draft,
            live_runs=live_runs,
            # The standing profile (#527): the memory view reads/edits/clears it here.
            profile=profile_store,
            max_upload_bytes=settings.attachment_max_bytes,
            allowed_upload_types=settings.attachment_allowed_type_list,
        )
    )
    app.include_router(create_readiness_router(readiness))
    app.include_router(create_system_router(gateway))
    app.include_router(create_modules_router(registry))
    app.include_router(create_suggestions_router(registry))
    app.include_router(create_calendar_feed_router(registry))
    # Chat-bridge admin (#369, ADR-0062): connect/manage the messaging module's bridges. The core
    # writes per-tenant bot tokens to OpenBao and reloads the module so a bridge connects at
    # runtime; the browser never holds a token (constraint #6).
    app.include_router(
        create_messaging_router(
            BridgeAdmin(
                secrets,
                RegistryBridgeClient(registry),
                tenant=settings.default_tenant_id,
            )
        )
    )
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
