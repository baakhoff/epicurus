"""Settings for the core runtime — CoreSettings plus the core-only LLM-gateway knobs."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from pydantic import field_validator

from epicurus_core import CoreSettings, get_logger
from epicurus_core.files import FileStoreBackend

log = get_logger(__name__)


class CoreAppSettings(CoreSettings):
    """Adds the LLM-gateway configuration to the shared settings."""

    # Ollama, the local LLM runtime. On the internal Docker network: http://ollama:11434.
    ollama_url: str = "http://localhost:11434"
    # KV-cache apply (#307): the core writes Ollama's start-up env file here (a named volume
    # both containers share; the Ollama entrypoint sources it), then restarts this service so it
    # re-reads it. Defaults match the compose wiring; override only if you remap either.
    ollama_runtime_env_path: str = "/etc/epicurus/ollama.env"
    ollama_service_name: str = "ollama"
    # Default Ollama model used when a request does not name one.
    llm_default_model: str = "llama3.2"
    # How long Ollama keeps a model loaded after its last use (idle unload, ADR-0005).
    llm_keep_alive: str = "5m"
    # Comma-separated fallback models, tried in order when the primary fails or is
    # unavailable (e.g. "claude/claude-3-5-sonnet-latest,gpt/gpt-4o").
    llm_fallbacks: str = ""
    # Per-model retries on 429 / 5xx (exponential backoff), handled by LiteLLM.
    llm_num_retries: int = 2
    # Inter-chunk read timeout (seconds) for LLM calls — the max gap between tokens before the
    # socket read aborts (#453). Sized for LOCAL inference: on a single GPU the pre-first-token
    # window (a cold model load + prompt-eval, possibly with an embed↔chat model swap) legitimately
    # stalls token flow for minutes; too low a value aborts a valid generation mid-stream with
    # "Timeout on reading data from socket". Default is generous (30 min) so a long knowledge-doc
    # generation runs to completion; lower it for faster failure, or set `0` to remove the
    # inter-chunk bound entirely (wait as long as the runtime needs). Governs the read timeout;
    # the connect timeout stays short (LLM_CONNECT_TIMEOUT_S) so a down runtime still fails fast.
    llm_timeout: float = 1800.0
    # ── LLM tuning (optional; None = use the provider/runtime default) ──────────
    # These pass straight through to the LiteLLM call, so tuning needs no code
    # edit — set the env var and restart (extend this block to wire more knobs).
    # Sampling temperature, applied to each chat completion (local and hosted).
    llm_temperature: float | None = None
    # Nucleus-sampling top_p, applied to each chat completion (local and hosted).
    llm_top_p: float | None = None
    # Ollama context-window size (num_ctx); applied to local models only.
    llm_num_ctx: int | None = None
    # ── Model catalog (#269) ────────────────────────────────────────────────────
    # The core parses the browsable model list from this source on a schedule, so the
    # Models screen never ships a hand-maintained list. Defaults to the public Ollama
    # library; point it at a mirror for an air-gapped deployment. The on-demand quant-variant
    # lookup (#330) reads each model's tags page (``<base>/<family>/tags``) under the same base.
    llm_catalog_url: str = "https://ollama.com/library"
    # How often the background loop re-parses the source (seconds). The library changes
    # rarely, so this is hours, not minutes. Floored to 60s by the catalog.
    llm_catalog_refresh_seconds: int = 6 * 60 * 60
    # Cap on model families kept (the most-popular survive); 0 = unlimited.
    llm_catalog_max_models: int = 0
    # When false, no outbound fetch happens and the built-in seed is served as-is
    # (air-gapped builds). The endpoint and seed still work; only the refresh is skipped.
    llm_catalog_enabled: bool = True
    # Pause between the background GB-size fill's tags-page lookups (#571). The index page
    # publishes no on-disk sizes, so the fill walks the families most-popular-first, one
    # rate-limited request at a time (shared with the quant-variant lookup's per-family
    # cache). 0 disables the fill; sizes then arrive only via on-demand variant lookups.
    llm_catalog_size_fill_seconds: float = 30.0
    # Comma-separated module base URLs. Each module serves its MCP tools at
    # <base>/mcp (the agent calls these) and its manifest at <base>/manifest
    # (the registry + web shell read these).
    module_urls: str = (
        "http://echo:8080,"
        "http://storage:8080,"
        "http://knowledge:8080,"
        "http://websearch:8080,"
        "http://calendar:8080,"
        "http://mail:8080,"
        "http://tasks:8080,"
        "http://notes:8080,"
        "http://messaging:8080"
    )
    # Max tool-calling rounds in one agent turn before it must answer.
    agent_max_steps: int = 4
    # ── Inbound messaging / chat bridges (ADR-0058) ─────────────────────────────
    # The core's inbound consumer subscribes ``<tenant>.messaging.inbound``, runs a headless
    # turn per bridge message, and publishes the reply to ``messaging.outbound`` (the
    # ``messaging`` module delivers it). Disable to run a build with no bridge path.
    messaging_inbound_enabled: bool = True
    # Optional dedicated model for bridge turns (e.g. a smaller/faster model for chat apps).
    # Blank → the gateway resolves the operator's default chat model, same as the web.
    messaging_model: str = ""
    # How long a turn paused by `ask_user` (ADR-0053) waits for an answer before its suspended
    # run is reaped (hours). If it expires the model can simply ask again on the next turn.
    ask_user_ttl_hours: int = 24
    # How long a draft paused for review (ADR-0085, #563) waits for the operator's Confirm/Decline
    # before its pending-draft run is reaped (hours). If it expires the model can compose again.
    draft_review_ttl_hours: int = 24
    # How long a *finished* in-flight run (#376) stays re-attachable in memory before it is
    # reaped (seconds). A reconnecting client within this window replays the buffered turn;
    # after it, the client falls back to the durable transcript. Pure cache — the answer is
    # already persisted, so this only bounds how long a late re-attach can tail the live buffer.
    live_run_grace_seconds: float = 300.0
    # Base URL of the module that durably keeps chat uploads (ADR-0025). The attachment
    # upload route best-effort POSTs each uploaded file's bytes to <url>/ingest so it
    # becomes browsable in the Files page. Empty disables the sink (e.g. an OSS build
    # without the storage module); a failed push never breaks the upload.
    attachment_sink_url: str = "http://storage:8080"
    # ── Chat-upload limits (#175) ───────────────────────────────────────────────
    # Max size of a single chat upload (POST /platform/v1/agent/attachments); above
    # this the route returns 413. Keep the web container's nginx client_max_body_size
    # at or above this so a legitimate upload reaches the core's clean JSON error.
    attachment_max_bytes: int = 10 * 1024 * 1024  # 10 MiB
    # Allowed upload content-types (comma-separated). Supports "type/*" wildcards and
    # "*/*" to disable the allowlist; a disallowed type is rejected with 415.
    attachment_allowed_types: str = "text/*,image/*,application/pdf,application/json"
    # Postgres DSN (async driver) for conversation persistence.
    database_url: str = "postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus"
    # Qdrant endpoint for semantic recall.
    qdrant_url: str = "http://localhost:6333"
    # Ollama embedding model used to vectorize conversation text for recall.
    memory_embed_model: str = "nomic-embed-text"
    # ── Background fact extraction (ADR-0045 / ADR-0051) ─────────────────────────
    # When the agent distils durable user facts from a finished exchange:
    #   "nightly"   — (default) defer each exchange to a durable queue, drained once a day at
    #                 memory_extraction_hour, so extraction never competes with a live turn for
    #                 the local GPU (the fix for memory work blocking chat);
    #   "immediate" — run it as a background task right after the turn (original ADR-0045).
    memory_extraction_mode: str = "nightly"
    # Local hour (0-23) of the nightly drain, in the operator's configured timezone (the Settings
    # timezone, else default_timezone). 3 = 3 AM.
    memory_extraction_hour: int = 3
    # Optional dedicated model for the extraction LLM call (e.g. "llama3.2:3b"). A small, fast
    # model keeps the nightly pass cheap and leaves the chat model untouched. Blank = the
    # operator's default chat model.
    memory_extraction_model: str = ""
    # Max exchanges distilled per nightly drain (a safety bound on one run's cost).
    memory_extraction_batch_limit: int = 200
    # Seconds recall (a single embedding round-trip) may take before a turn proceeds without it.
    # Recall is the one memory step still on the response path; bounding it keeps a cold embedder
    # from stalling the first token (ADR-0051). 4s (was 2s) so a single-GPU embed-model swap can
    # finish — at 2s recall timed out on nearly every turn. Raise on slow hardware; lower if the
    # embed model is kept warm.
    memory_recall_timeout_s: float = 4.0
    # ── Standing profile (ADR-0094) ─────────────────────────────────────────────
    # A compact per-tenant profile of the user, synthesized from the fact store on the nightly
    # maintenance batch and injected STATICALLY in _assemble (no turn-time embed) — moving the
    # common-case recall cost off the response path (the same trade ADR-0051 made for extraction).
    # Optional dedicated model for the synthesis LLM call (blank = the operator's default chat
    # model); a small model keeps the nightly pass cheap.
    memory_profile_model: str = ""
    # How many past profile versions to retain per tenant (the newest is injected). Mirrors the
    # editor's MAX_VERSIONS idiom (ADR-0046) — enough to browse/restore, bounded so it can't grow.
    memory_profile_max_versions: int = 5
    # Optional dedicated model for the nightly playbook-reflection call (ADR-0093 §1; blank = the
    # operator's default chat model). Mirrors `memory_profile_model` — a small model keeps the
    # nightly pass cheap. There is deliberately no reflection *hour* knob: the pass rides the
    # maintenance orchestrator's single schedule (ADR-0093 §1, ADR-0098), never one of its own.
    playbook_reflection_model: str = ""
    # Default IANA timezone the agent's `now` tool reports when the operator hasn't set one
    # in Settings (e.g. "Europe/Belgrade"). UTC keeps the OSS default neutral; each
    # deployment sets its own in the Settings screen (persisted) or via this env.
    default_timezone: str = "UTC"

    # ── Maintenance orchestrator (ADR-0060/#621) ────────────────────────────────
    # The "run all background maintenance" trigger (memory extraction, module re-index) is always
    # available manually. Its nightly *schedule* is opt-in: the per-runner schedules already cover
    # the unattended case, so this defaults off to avoid redundant nightly work — turn it on (here,
    # or per-tenant at runtime in Settings, #621) to run one coordinated light batch (consolidating
    # the per-runner schedules onto it is the follow-up). These two env vars are only the *default*
    # a tenant inherits until it sets its own schedule via PUT /platform/v1/maintenance/schedule.
    maintenance_schedule_enabled: bool = False
    # Local hour (0-23) of the scheduled nightly batch, in the operator's timezone. 4 = 4 AM, an
    # hour after the extraction drain's default so the two never overlap when both are on. Only
    # meaningful for the default daily cadence — a tenant's own weekly/hourly override supplies
    # its own hour/weekday.
    maintenance_hour: int = 4
    # How often the schedule is checked for due-ness (#621) — a plain poll, like scheduled turns,
    # since the schedule is now runtime-editable and a fixed sleep-until-hour can't react to a
    # change made while it's sleeping. 60s keeps the worst-case delivery lag under a minute.
    maintenance_poll_interval_s: int = 60

    # ── Scheduled turns (ADR-0092) ────────────────────────────────────────────────
    # How often the scheduler checks for a due row (a plain poll, not one sleep-until-hour task
    # per row — rows are created/paused/deleted at runtime with independently configured hours).
    # 60s keeps the worst-case delivery lag under a minute without meaningfully polling Postgres.
    scheduled_turns_poll_interval_s: int = 60

    # ── File space (ADR-0052) ───────────────────────────────────────────────────
    # The core owns the per-tenant user file space behind a swappable backend (constraint #3).
    # "local" serves the shared volume at FILES_ROOT/<tenant>; "s3" serves a per-tenant
    # {tenant}-files bucket on the MinIO/S3 endpoint. Modules consume it via the
    # /platform/v1/files/* API (PlatformClient.files_*) rather than mounting the volume.
    files_backend: FileStoreBackend = "local"
    files_root: Path = Path("/data")
    files_s3_url: str = "http://minio:9000"
    files_s3_access_key: str = "epicurus"
    files_s3_secret_key: str = "epicurus-dev"
    # Live file-index sync (ADR-0063): watch the local file-space tree and incrementally rescan
    # on change, so a file written through the file API, an external write, or an Obsidian-Sync
    # drop shows up in the Files view and search without a restart. Local backend only (S3 has
    # no inotify surface). On by default; FILES_WATCH=false disables it for a huge tree.
    files_watch: bool = True
    # Coalescing window (ms) for a burst of changes before a rescan fires — a module dropping
    # many files at once is grouped into one incremental pass. Passed to the watcher's debounce.
    files_watch_debounce_ms: int = 1500

    # ── Push notifications (ADR-0102) ───────────────────────────────────────────
    # The contact identity presented in the VAPID JWT (RFC 8292) — a mailto: or https: URL
    # a push service can use to reach the operator about this application server. The
    # default is a neutral placeholder, not a working inbox; set it for a real deployment.
    push_vapid_subject: str = "mailto:admin@example.com"
    # Max push notifications delivered per tenant per hour, across every category and
    # device — a blunt, in-memory (single-instance v1) safety valve independent of quiet
    # hours. A notification beyond the cap is still recorded (#671's durable center) but
    # not pushed. 0 disables the cap.
    push_rate_cap_per_hour: int = 30
    # How often the quiet-hours digest scheduler checks whether a tenant's quiet window has
    # just ended (a plain poll, mirroring maintenance/scheduled-turns — see their settings).
    push_quiet_poll_interval_s: int = 60

    # ── OAuth settings ────────────────────────────────────────────────────────
    # Public base URL of the server used to build the OAuth redirect_uri.
    # Must exactly match the URI registered with each OAuth provider.
    # Example: http://localhost:8084 (local web port), https://epicurus.example.com
    oauth_redirect_base_url: str = "http://localhost:8084"
    # HMAC key for signing the OAuth ``state`` parameter (CSRF protection).
    # Change this before first use; rotating it invalidates in-flight connect flows.
    oauth_state_secret: str = "change-this-before-use"

    @field_validator("llm_temperature", "llm_top_p", "llm_num_ctx", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Treat a blank env value (e.g. ``LLM_TEMPERATURE=``) as unset.

        Compose passes these as ``${LLM_TEMPERATURE:-}``; an empty string would
        otherwise fail numeric parsing instead of falling back to the default.
        """
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("llm_timeout", mode="before")
    @classmethod
    def _blank_timeout_to_default(cls, value: object) -> object:
        """Treat a blank ``LLM_TIMEOUT=`` as the default rather than failing float parsing.

        Unlike the tuning knobs above, this is a non-optional float, so a blank env value must
        fall back to the class default instead of ``None``.
        """
        if isinstance(value, str) and value.strip() == "":
            return cls.model_fields["llm_timeout"].default
        return value

    @property
    def fallback_models(self) -> list[str]:
        """The fallback chain parsed from ``llm_fallbacks``."""
        return [m.strip() for m in self.llm_fallbacks.split(",") if m.strip()]

    @property
    def defer_extraction(self) -> bool:
        """Whether fact extraction is deferred to the nightly runner (vs. run immediately)."""
        return self.memory_extraction_mode.strip().lower() != "immediate"

    @property
    def module_base_urls(self) -> list[str]:
        """The module base URLs parsed from ``module_urls``."""
        return [u.strip().rstrip("/") for u in self.module_urls.split(",") if u.strip()]

    @property
    def module_hostnames(self) -> list[str]:
        """The bare module hostnames from ``module_urls`` (e.g. ``knowledge``).

        By convention (ADR-0063) a module's hostname is also the top-level file-space
        folder it owns — the Files page treats those subtrees as locked: not movable
        from the UI and not an upload destination (#479).
        """
        hosts: list[str] = []
        for base in self.module_base_urls:
            host = urlparse(base).hostname
            if host is None:
                # A scheme-less entry ("knowledge:8080") parses its host *as* the URL scheme,
                # leaving hostname None — which would silently unlock that module's folder
                # (#554). Recover the host by re-parsing with a "//" authority prefix; warn
                # either way so a malformed config is visible instead of silently degrading.
                host = urlparse(f"//{base}").hostname
                if host:
                    log.warning(
                        "module_urls entry has no scheme; assuming host to keep its folder locked",
                        entry=base,
                        host=host,
                    )
                else:
                    log.warning(
                        "module_urls entry has no resolvable host; its folder will not be locked",
                        entry=base,
                    )
            if host:
                hosts.append(host)
        return hosts

    @property
    def module_mcp_urls(self) -> list[str]:
        """Each module's MCP endpoint (``<base>/mcp``)."""
        return [f"{base}/mcp" for base in self.module_base_urls]

    @property
    def attachment_allowed_type_list(self) -> list[str]:
        """The upload content-type allowlist parsed from ``attachment_allowed_types``."""
        return [t.strip().lower() for t in self.attachment_allowed_types.split(",") if t.strip()]
