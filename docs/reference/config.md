# Reference: `config`

`epicurus_core.config` — environment-driven, **non-secret** configuration shared
by every service. Secrets do not belong here; they come from
[`SecretStore`](secrets.md).

## `CoreSettings`

```python
class CoreSettings(pydantic_settings.BaseSettings)
```

The base settings every service shares. Loads from environment variables and an
optional local `.env` (case-insensitive; unknown vars ignored). **Subclass it** to
add service-specific fields.

### Fields

| Field | Env var | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `service_name` | `SERVICE_NAME` | `str` | `"epicurus"` | Identity of the running service. |
| `app_env` | `APP_ENV` | `Environment` | `"local"` | `local` / `staging` / `production`. |
| `log_level` | `LOG_LEVEL` | `LogLevel` | `"info"` | `debug` … `critical`. |
| `json_logs` | `JSON_LOGS` | `bool \| None` | `None` | Force JSON logs; `None` = decide by env. |
| `default_tenant_id` | `DEFAULT_TENANT_ID` | `str` | `"local"` | Tenant for self-host (validated). |
| `nats_url` | `NATS_URL` | `str` | `nats://localhost:4222` | Event bus — see [`events`](events.md). |
| `nats_user` | `NATS_USER` | `str \| None` | `None` | Bus auth role (`core` / `module`); `None` connects anonymously. See [NATS](../infrastructure/nats.md). |
| `nats_password` | `NATS_PASSWORD` | `str \| None` | `None` | Bus auth password for the role (the server-side `NATS_{CORE,MODULE,SYS}_PASSWORD` it matches are documented in [NATS](../infrastructure/nats.md)). |
| `openbao_url` | `OPENBAO_URL` | `str` | `http://localhost:8200` | Secrets — see [`secrets`](secrets.md). |
| `openbao_token` | `OPENBAO_TOKEN` | `str \| None` | `None` | Bootstrap token, injected at runtime. |
| `openbao_token_file` | `OPENBAO_TOKEN_FILE` | `str \| None` | `None` | Path to a file holding the token (e.g. a mounted Docker secret); used when `openbao_token` is unset. |
| `otel_traces_enabled` | `OTEL_TRACES_ENABLED` | `bool` | `False` | Emit OpenTelemetry traces to Tempo — see [tracing](observability.md#tracing-57-adr-0068). |
| `otel_exporter_otlp_endpoint` | `OTEL_EXPORTER_OTLP_ENDPOINT` | `str` | `http://tempo:4318` | OTLP/HTTP base URL for traces; the exporter appends `/v1/traces`. |

### Properties & methods

- **`is_production -> bool`** — `True` when `app_env == "production"`.
- **`use_json_logs -> bool`** — whether logs render as JSON: the `json_logs`
  override when set, otherwise `True` everywhere except `local`.
- **`resolve_openbao_token() -> str | None`** — the bootstrap token: the
  explicit `openbao_token` when set, else the stripped content of
  `openbao_token_file`, else `None`. Used by `SecretStore.from_settings`.

### Validation

`default_tenant_id` is validated with [`is_valid_tenant_id`](tenancy.md); an
invalid value raises `pydantic.ValidationError` when settings load.

### Example

```python
from epicurus_core import CoreSettings

class GreeterSettings(CoreSettings):
    greeting: str = "Hello"          # add your own fields

settings = GreeterSettings()         # reads env / .env
settings.greeting, settings.is_production, settings.use_json_logs
```

## `CoreAppSettings`

```python
class CoreAppSettings(CoreSettings)
```

The **core runtime** service's settings (`epicurus_core_app.settings`) — everything
in `CoreSettings` plus the LLM-gateway, agent, module, and memory knobs.

### Fields (in addition to `CoreSettings`)

| Field | Env var | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `ollama_url` | `OLLAMA_URL` | `str` | `http://localhost:11434` | Local LLM runtime (the stack reaches it at `http://ollama:11434`). |
| `ollama_runtime_env_path` | `OLLAMA_RUNTIME_ENV_PATH` | `str` | `/etc/epicurus/ollama.env` | Where the core writes Ollama's start-up env file (KV-cache type) for it to source on restart (#307). A shared volume; override only if you remap the mount. |
| `ollama_service_name` | `OLLAMA_SERVICE_NAME` | `str` | `ollama` | Compose service the core restarts to apply a KV-cache change (#307). |
| `llm_default_model` | `LLM_DEFAULT_MODEL` | `str` | `llama3.2` | Model used when a request names none. |
| `llm_keep_alive` | `LLM_KEEP_ALIVE` | `str` | `5m` | How long Ollama keeps a model loaded after use (ADR-0005). |
| `llm_fallbacks` | `LLM_FALLBACKS` | `str` | `""` | Comma-separated fallback models, tried in order (e.g. `claude/claude-3-5-sonnet-latest,gpt/gpt-4o`). |
| `llm_num_retries` | `LLM_NUM_RETRIES` | `int` | `2` | Per-model retries on 429/5xx (LiteLLM backoff). |
| `llm_timeout` | `LLM_TIMEOUT` | `float` | `1800.0` (30 min) | Inter-chunk read timeout (seconds) for LLM calls — the max gap between tokens before the socket read aborts (#453). Sized for local inference: a cold model-load / prompt-eval stall (worst on the first long generation after tool/embed activity) can pause token flow for minutes; too low a value aborts a valid generation mid-stream with `Timeout on reading data from socket`. Lower it for faster failure, or set `0` to remove the inter-chunk bound entirely. The connect timeout stays short (30s) so a down runtime still fails fast. |
| `llm_temperature` | `LLM_TEMPERATURE` | `float \| None` | `None` | Sampling temperature passed to each chat completion (local + hosted). A blank env value means unset. |
| `llm_top_p` | `LLM_TOP_P` | `float \| None` | `None` | Nucleus-sampling `top_p` passed to each chat completion (local + hosted). |
| `llm_num_ctx` | `LLM_NUM_CTX` | `int \| None` | `None` | Ollama context-window size (`num_ctx`); applied to local models only. |
| `llm_catalog_url` | `LLM_CATALOG_URL` | `str` | `https://ollama.com/library` | Source the core parses the browsable model catalog from (#269). Point at a mirror for an air-gapped deployment. |
| `llm_catalog_refresh_seconds` | `LLM_CATALOG_REFRESH_SECONDS` | `int` | `21600` (6h) | How often the background loop re-parses the catalog source. Floored to 60s. |
| `llm_catalog_max_models` | `LLM_CATALOG_MAX_MODELS` | `int` | `0` | Cap on model families kept (the most-popular survive); `0` = unlimited. |
| `llm_catalog_enabled` | `LLM_CATALOG_ENABLED` | `bool` | `true` | When false, no outbound fetch — the built-in seed is served as-is (the endpoint still works). |
| `llm_catalog_size_fill_seconds` | `LLM_CATALOG_SIZE_FILL_SECONDS` | `float` | `30.0` | Pause between the background GB-size fill's per-family tags-page lookups (#571) — the rate limit on the walk that backfills catalog `size_gb` (most-popular first, shared with the variant lookup's cache). `0` disables the fill; sizes then arrive only via on-demand variant lookups. Never fetches when the catalog is disabled. |
| `module_urls` | `MODULE_URLS` | `str` | `http://echo:8080` | Comma-separated module base URLs; each serves MCP at `<base>/mcp` and its manifest at `<base>/manifest`. |
| `agent_max_steps` | `AGENT_MAX_STEPS` | `int` | `4` | **Default** max tool-calling rounds per agent turn; the operator can override it at runtime (`llm_prefs.agent_max_steps`, set via the Models/Settings UI — #297) without a restart. |
| `messaging_inbound_enabled` | `MESSAGING_INBOUND_ENABLED` | `bool` | `true` | Whether the core's inbound-messaging consumer runs — subscribes `<tenant>.messaging.inbound`, runs a headless turn per bridge message, and publishes the reply to `messaging.outbound` (ADR-0058). Disable for a build with no chat-bridge path. |
| `messaging_model` | `MESSAGING_MODEL` | `str` | `""` | Optional dedicated model for bridge turns (e.g. a smaller/faster model for chat apps); blank = the gateway resolves the operator's default chat model, same as the web. |
| `attachment_sink_url` | `ATTACHMENT_SINK_URL` | `str` | `http://storage:8080` | Base URL of the module that durably keeps chat uploads (ADR-0025); the upload route best-effort POSTs bytes to `<url>/ingest`. Empty disables the sink (uploads stay core-side only); a failed push never breaks the upload. |
| `attachment_max_bytes` | `ATTACHMENT_MAX_BYTES` | `int` | `10485760` (10 MiB) | Max size of a single chat upload; the route returns **413** above it. Keep the web container's nginx `client_max_body_size` at or above this. |
| `attachment_allowed_types` | `ATTACHMENT_ALLOWED_TYPES` | `str` | `text/*,image/*,application/pdf,application/json` | Allowed upload content-types (comma-separated; `type/*` wildcards, `*/*` disables the allowlist). A disallowed type is rejected with **415**. |
| `database_url` | `DATABASE_URL` | `str` | `postgresql+asyncpg://…/epicurus` | Postgres DSN for conversation persistence. |
| `qdrant_url` | `QDRANT_URL` | `str` | `http://localhost:6333` | Qdrant endpoint for semantic recall. |
| `memory_embed_model` | `MEMORY_EMBED_MODEL` | `str` | `nomic-embed-text` | Local embedding model used for recall. |
| `memory_extraction_mode` | `MEMORY_EXTRACTION_MODE` | `str` | `nightly` | When durable-fact extraction runs: `nightly` (defer each exchange to a queue drained off-hours, ADR-0051) or `immediate` (a background task right after the turn, ADR-0045). Any value other than `immediate` means deferred. |
| `memory_extraction_hour` | `MEMORY_EXTRACTION_HOUR` | `int` | `3` | Local hour (0-23) of the nightly drain, in the operator's configured timezone (the Settings timezone, else `DEFAULT_TIMEZONE`). |
| `memory_extraction_model` | `MEMORY_EXTRACTION_MODEL` | `str` | `""` | Optional dedicated model for the extraction LLM call (e.g. `llama3.2:3b`) — a small, fast model keeps the nightly pass cheap and off the chat model. Blank = the operator's default chat model. |
| `memory_extraction_batch_limit` | `MEMORY_EXTRACTION_BATCH_LIMIT` | `int` | `200` | Max exchanges distilled per nightly drain (a safety bound on one run's cost). |
| `memory_recall_timeout_s` | `MEMORY_RECALL_TIMEOUT_S` | `float` | `4.0` | Seconds the inline recall embed may take before a turn proceeds without it — bounds the only memory step still on the response path (ADR-0051). 4s (was 2s) lets a single-GPU embed-model swap finish; on timeout the turn proceeds with no recall and logs `recall skipped: embed timed out`, while a backend failure logs `recall skipped: backend error` (with the exception type) so the two are distinguishable. |
| `memory_profile_model` | `MEMORY_PROFILE_MODEL` | `str` | `""` | Optional dedicated model for the nightly **standing-profile** synthesis (ADR-0094) — a small model keeps the pass cheap. Blank = the operator's default chat model. |
| `playbook_reflection_model` | `PLAYBOOK_REFLECTION_MODEL` | `str` | `""` | Optional dedicated model for the nightly **playbook-reflection** pass (ADR-0093 §1) — a small model keeps the pass cheap. Blank = the operator's default chat model. There is deliberately no reflection *hour* knob: the pass rides the maintenance orchestrator's single schedule (ADR-0098), never one of its own. |
| `memory_profile_max_versions` | `MEMORY_PROFILE_MAX_VERSIONS` | `int` | `5` | How many past standing-profile versions to retain per tenant (the newest is injected each turn). |
| `default_timezone` | `DEFAULT_TIMEZONE` | `str` | `UTC` | Fallback IANA timezone the agent's `now` tool reports when the operator hasn't set one in Settings (ADR-0039). |
| `maintenance_schedule_enabled` | `MAINTENANCE_SCHEDULE_ENABLED` | `bool` | `false` | **Default only** (#621, ADR-0098) — a tenant inherits this until it sets its own schedule via `PUT /platform/v1/maintenance/schedule`. Off by default: the per-runner schedules already cover the unattended case; the manual "run everything" trigger is always available regardless. |
| `maintenance_hour` | `MAINTENANCE_HOUR` | `int` | `4` | **Default only**, paired with the row above — the daily-cadence hour (0-23, operator's timezone) a tenant inherits until it sets its own schedule. An hour after `MEMORY_EXTRACTION_HOUR` so the two never overlap when both are on. |
| `maintenance_poll_interval_s` | `MAINTENANCE_POLL_INTERVAL_S` | `int` | `60` | How often the maintenance orchestrator re-checks its schedule for due-ness (#621) — a plain poll (like scheduled turns), since the schedule is now editable at runtime and a fixed sleep-until-hour can't react to a mid-sleep change. Bounds the worst-case delivery lag. |
| `scheduled_turns_poll_interval_s` | `SCHEDULED_TURNS_POLL_INTERVAL_S` | `int` | `60` | How often the scheduled-turns poll loop checks for a due row (ADR-0092) — a plain poll, not one `sleep_until_hour` task per row, since rows are created/paused/deleted at runtime with independently configured hours. Keeps worst-case delivery lag under a minute without meaningfully polling Postgres. |
| `events_retention_days` | `EVENTS_RETENTION_DAYS` | `int` | `30` | How long the core keeps a module event in its durable log (ADR-0103). The bus replays nothing, so the log is the copy of record and grows with the chattiest emitter. Days, not rows — the log answers "what happened recently", and an operator reasons in days. `0` or negative disables pruning entirely: keep everything, and mind the disk. See [events](events.md). |
| `events_prune_interval_s` | `EVENTS_PRUNE_INTERVAL_S` | `int` | `3600` | How often the event-log pruner sweeps. Retention is a window, not a deadline — an event lingering an extra hour past the cutoff costs nothing — so this is hourly rather than tight. |
| `files_backend` | `FILES_BACKEND` | `str` | `local` | Core-owned file-space backend (ADR-0052): `local` (filesystem) or `s3` (MinIO/S3). See [file space](files.md). |
| `files_root` | `FILES_ROOT` | `str` | `/data` | Local-backend base; the tenant file tree is `FILES_ROOT/<tenant>`. |
| `files_s3_url` | `FILES_S3_URL` | `str` | `http://minio:9000` | S3 endpoint when `files_backend=s3`. |
| `files_s3_access_key` / `files_s3_secret_key` | `FILES_S3_ACCESS_KEY` / `FILES_S3_SECRET_KEY` | `str` | `epicurus` / `epicurus-dev` | S3 credentials (dev defaults; OpenBao later). |
| `files_watch` | `FILES_WATCH` | `bool` | `true` | Watch the mounted file space and **incrementally rescan on change** so files landed after startup show in the core Files page and search without a restart (ADR-0063). On by default. Set `false` for startup-only scanning. |
| `files_watch_debounce_ms` | `FILES_WATCH_DEBOUNCE_MS` | `int` | `1500` | Coalescing window (ms) for a burst of file changes before a watch-triggered rescan fires. |
| `push_vapid_subject` | `PUSH_VAPID_SUBJECT` | `str` | `mailto:admin@example.com` | Contact identity in the VAPID JWT (RFC 8292) — a `mailto:`/`https:` URL a push service can use to reach the operator. Set it for a real deployment (#670, ADR-0102); the default is a neutral placeholder, not a working inbox. |
| `push_rate_cap_per_hour` | `PUSH_RATE_CAP_PER_HOUR` | `int` | `30` | Max push notifications delivered per tenant per hour, across every category/device — in-memory, single-instance v1 (ADR-0102 §3). `0` disables the cap. |
| `push_quiet_poll_interval_s` | `PUSH_QUIET_POLL_INTERVAL_S` | `int` | `60` | How often the quiet-hours digest scheduler checks whether a tenant's quiet window just ended (a plain poll, mirroring maintenance/scheduled-turns). |

### Properties

- **`fallback_models -> list[str]`** — the `llm_fallbacks` chain, parsed.
- **`module_base_urls -> list[str]`** — the `module_urls`, trimmed of a trailing `/`.
- **`module_mcp_urls -> list[str]`** — each module's `<base>/mcp` endpoint.
- **`attachment_allowed_type_list -> list[str]`** — the `attachment_allowed_types`, parsed + lowercased.
- **`defer_extraction -> bool`** — whether fact extraction is deferred to the nightly runner (`memory_extraction_mode` is anything but `immediate`).

## Shared file space (per-module storage roots)

The **core** plus the file-owning module **knowledge** share **one** file tree — the
*shared file space* (#KB-refactor). One deployment-level env var mounts it; the settings below
are the in-container paths under it. Since the file-space migration Phase 2 (ADR-0063) the
**core** mounts the volume and owns the file index + the unified Files browser; **storage no
longer mounts `/data`** (it reads the file space through the core file API — see
[file space](files.md)), so its in-container root and scan/watch knobs are gone. Since Phase 4
(#357/ADR-0065) **notes no longer mounts `/data`** either — it writes its `.md` mirror through
the core file API, so only the core and knowledge still bind the volume.

| Env var | Default | Scope | Meaning |
| --- | --- | --- | --- |
| `EPICURUS_FILES_ROOT` | empty named volume (`epicurus-files`) | compose | The host path (or named volume) bound at `/data` for the **core** (the file index, read-write). It is now **mounted only by the core** — storage (ADR-0063), notes (#357/ADR-0065), and knowledge (#346/ADR-0070) all reach the file space through the core file API instead. Point it at a host directory to expose real files; never the host home dir. **Replaces** the old per-module `KNOWLEDGE_HOST_VAULT` and `STORAGE_HOST_ROOT`. The on-disk tree is tenant-scoped: the **core image's entrypoint** creates and chowns `/data/<tenant>` to uid 10001 (`<tenant>` = `DEFAULT_TENANT_ID`) before dropping privileges, so the core owns the tenant root and writes the module subtrees (#421/ADR-0069, replacing the old `files-init` one-shot) — see [Infrastructure](../infrastructure/index.md#shared-file-space). Knowledge re-mounts it read-only **only in watch mode** (`VAULT_WATCH=true`) via a compose override — see [Obsidian sync](../developer/obsidian-sync.md). |
| `FILES_WATCH` | `true` | core-app | Watch the mounted file space and **incrementally rescan on change** (create/modify/delete), so files landed after startup show in the core Files page and name search without a restart (ADR-0063). On by default. Set `false` for startup-only scanning. Also listed under [`CoreAppSettings`](#coreappsettings). |
| `FILES_WATCH_DEBOUNCE_MS` | `1500` | core-app | Coalescing window (ms) for a burst of file changes before a watch-triggered rescan fires; a module dropping many files at once is grouped into one incremental pass. |
| `PLATFORM_URL` | `http://core-app:8080` | storage, knowledge, notes | The core base URL for the file API (`PlatformClient.files_*`). **Storage** reads the file space through it — storage no longer mounts `/data` (ADR-0063). **Knowledge** reads *and* writes through it and mounts **no** `/data` in normal mode (writes since #356/ADR-0064, reads since #346/ADR-0070; watch mode is the disk-mount exception); it also uses it for embeddings. **Notes** writes its `.md` mirror through it and mounts **no** `/data` at all (since #357/ADR-0065); notes also already used it for embeddings. |
| `STORAGE_AGENT_HIDDEN_PREFIXES` | `notes` | storage | Comma-separated top-level subtrees hidden from the **agent's** file tools (`storage_list`/`storage_search`/`storage_read`); the operator-facing core Files surface is unaffected (#KB-refactor, storage v0.5.0). `notes/` holds private note bodies the agent must not read. Set empty to hide nothing. |
| `VAULT_PATH` | `/data/knowledge` | knowledge | Knowledge's path within the shared file space; the tree is tenant-scoped to `<files-root>/<tenant>/knowledge`, and each top-level folder under it is a project (knowledge base). Was `/vault` before #KB-refactor. Knowledge holds **no `/data` mount** in normal mode: both reads and writes go through the core file API (`PlatformClient.files_*`, core key `knowledge/<rel>` — writes #356/ADR-0064, reads #346/ADR-0070). `VAULT_PATH` remains the in-container key prefix — its `.name` (`knowledge`) is the core prefix. Watch mode (`VAULT_WATCH=true`) re-mounts this path from disk via an override. |
| `NOTES_ROOT` | `/data/notes` | notes | The **logical** base for the `.md` mirror's path mapping — **not a mount** (since File-space Phase 4, #357/ADR-0065). A note slug maps to the core file-API path `notes/<slug>.md`; the **core** writes it on disk into `<files-root>/<tenant>/notes`. Each saved note is mirrored as `<slug>.md`; Postgres stays the source of truth (#KB-refactor). |

The on-disk file tree is **tenant-scoped** (constraint #1): the core indexes
`<files-root>/<tenant>`, and knowledge and notes build their roots as
`<files-root>/<tenant>/{knowledge,notes}`, where `<tenant>` is `DEFAULT_TENANT_ID` (default
`local`). The volume mount stays `/data` — only the in-container path carries the tenant segment.

> **Removed (ADR-0063).** `STORAGE_ROOT`, `STORAGE_WATCH`, and `STORAGE_WATCH_DEBOUNCE_MS` are
> gone — storage no longer mounts or scans the file space; the core does (behind `FILES_WATCH` /
> `FILES_WATCH_DEBOUNCE_MS`). Existing storage deployments drop those env vars and set
> `PLATFORM_URL`.

**Migration note for existing deployments.** `EPICURUS_FILES_ROOT` replaces
`KNOWLEDGE_HOST_VAULT` and `STORAGE_HOST_ROOT`. Move old vault contents into
`<files-root>/<tenant>/knowledge/<project>/` (`<tenant>` = `DEFAULT_TENANT_ID`, default
`local`; each project is a top-level folder) so they appear as knowledge bases.

## Docker-socket opt-in (#622, ADR-0099)

Not a `CoreAppSettings` field — read directly by `services/core-app/docker-entrypoint.py`
(the container's root-run entrypoint) before the app process even starts, so it can't go
through pydantic-settings like the rest of this page.

| Env var | Default | Scope | Meaning |
| --- | --- | --- | --- |
| `DOCKER_GID` | unset | core-app entrypoint | The host's docker-socket group id. When set, the entrypoint joins it (via `setgroups`, while still root) before dropping to the unprivileged app uid — the missing half of the opt-in `services/core-app/compose.docker-socket.yaml` overlay, which mounts `/var/run/docker.sock` but cannot by itself make it reachable by a non-root process. Unset (the default): the socket, if mounted at all, stays unreachable — module removal and the Ollama KV-cache restart still work, just deferred to the next restart (see [modules](modules.md#removing-a-module--tombstone-now-tear-the-container-down-out-of-band-127-382-adr-0028)). Find your host's value with `getent group docker \| cut -d: -f3` or `stat -c '%g' /var/run/docker.sock`. |

## Type aliases

```python
Environment = Literal["local", "staging", "production"]
LogLevel = Literal["debug", "info", "warning", "error", "critical"]
```
