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
| `openbao_url` | `OPENBAO_URL` | `str` | `http://localhost:8200` | Secrets — see [`secrets`](secrets.md). |
| `openbao_token` | `OPENBAO_TOKEN` | `str \| None` | `None` | Bootstrap token, injected at runtime. |
| `openbao_token_file` | `OPENBAO_TOKEN_FILE` | `str \| None` | `None` | Path to a file holding the token (e.g. a mounted Docker secret); used when `openbao_token` is unset. |

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
| `llm_temperature` | `LLM_TEMPERATURE` | `float \| None` | `None` | Sampling temperature passed to each chat completion (local + hosted). A blank env value means unset. |
| `llm_top_p` | `LLM_TOP_P` | `float \| None` | `None` | Nucleus-sampling `top_p` passed to each chat completion (local + hosted). |
| `llm_num_ctx` | `LLM_NUM_CTX` | `int \| None` | `None` | Ollama context-window size (`num_ctx`); applied to local models only. |
| `llm_catalog_url` | `LLM_CATALOG_URL` | `str` | `https://ollama.com/library` | Source the core parses the browsable model catalog from (#269). Point at a mirror for an air-gapped deployment. |
| `llm_catalog_refresh_seconds` | `LLM_CATALOG_REFRESH_SECONDS` | `int` | `21600` (6h) | How often the background loop re-parses the catalog source. Floored to 60s. |
| `llm_catalog_max_models` | `LLM_CATALOG_MAX_MODELS` | `int` | `0` | Cap on model families kept (the most-popular survive); `0` = unlimited. |
| `llm_catalog_enabled` | `LLM_CATALOG_ENABLED` | `bool` | `true` | When false, no outbound fetch — the built-in seed is served as-is (the endpoint still works). |
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
| `default_timezone` | `DEFAULT_TIMEZONE` | `str` | `UTC` | Fallback IANA timezone the agent's `now` tool reports when the operator hasn't set one in Settings (ADR-0039). |
| `maintenance_schedule_enabled` | `MAINTENANCE_SCHEDULE_ENABLED` | `bool` | `false` | Whether the maintenance orchestrator runs its **nightly** batch (ADR-0060). Off by default — the per-runner schedules already cover the unattended case; the manual "run everything" trigger is always available. Turn on for one coordinated nightly light batch. |
| `maintenance_hour` | `MAINTENANCE_HOUR` | `int` | `4` | Local hour (0-23) of the scheduled nightly maintenance batch, in the operator's timezone — an hour after `MEMORY_EXTRACTION_HOUR` so the two never overlap when both are on. |
| `files_backend` | `FILES_BACKEND` | `str` | `local` | Core-owned file-space backend (ADR-0052): `local` (filesystem) or `s3` (MinIO/S3). See [file space](files.md). |
| `files_root` | `FILES_ROOT` | `str` | `/data` | Local-backend base; the tenant file tree is `FILES_ROOT/<tenant>`. |
| `files_s3_url` | `FILES_S3_URL` | `str` | `http://minio:9000` | S3 endpoint when `files_backend=s3`. |
| `files_s3_access_key` / `files_s3_secret_key` | `FILES_S3_ACCESS_KEY` / `FILES_S3_SECRET_KEY` | `str` | `epicurus` / `epicurus-dev` | S3 credentials (dev defaults; OpenBao later). |

### Properties

- **`fallback_models -> list[str]`** — the `llm_fallbacks` chain, parsed.
- **`module_base_urls -> list[str]`** — the `module_urls`, trimmed of a trailing `/`.
- **`module_mcp_urls -> list[str]`** — each module's `<base>/mcp` endpoint.
- **`attachment_allowed_type_list -> list[str]`** — the `attachment_allowed_types`, parsed + lowercased.
- **`defer_extraction -> bool`** — whether fact extraction is deferred to the nightly runner (`memory_extraction_mode` is anything but `immediate`).

## Shared file space (per-module storage roots)

The file-owning modules (storage, knowledge, notes) share **one** file tree — the *shared
file space* (#KB-refactor). One deployment-level env var mounts it; the module settings below
are the in-container paths under it.

| Env var | Default | Scope | Meaning |
| --- | --- | --- | --- |
| `EPICURUS_FILES_ROOT` | empty named volume (`epicurus-files`) | compose | The host path (or named volume) bound at `/data` for storage (read-only), knowledge (read-write), and notes (read-write). Point it at a host directory to expose real files; never the host home dir. **Replaces** the old per-module `KNOWLEDGE_HOST_VAULT` and `STORAGE_HOST_ROOT`. The on-disk tree is tenant-scoped: a one-shot `files-init` container creates `/data/<tenant>/knowledge` + `/data/<tenant>/notes` (`<tenant>` = `DEFAULT_TENANT_ID`) and chowns them to uid 10001 (see [Infrastructure](../infrastructure/index.md#shared-file-space)). |
| `STORAGE_ROOT` | `/data` | storage | In-container **base** of the shared-file-space mount; storage serves and indexes the tenant subtree `STORAGE_ROOT/<tenant>` read-only (tenant-scoped, constraint #1). |
| `STORAGE_AGENT_HIDDEN_PREFIXES` | `notes` | storage | Comma-separated top-level subtrees (relative to the served tenant subtree) hidden from the **agent's** file tools (`storage_list`/`storage_search`/`storage_read`); the operator-facing Files page / `/read` / `/download` are unaffected (#KB-refactor, storage v0.5.0). `notes/` holds private note bodies the agent must not read. Set empty to hide nothing. |
| `STORAGE_WATCH` | `true` | storage | Watch the served tenant subtree and **incrementally rescan on change** (create/modify/delete), so files landed after startup show up in the Files page and name search without a restart or a manual `storage_rescan` (#390, storage v0.6.0, ADR-0057). On by default — it fixes a real stale-index bug. Set `false` to keep startup-only scanning. |
| `STORAGE_WATCH_DEBOUNCE_MS` | `1500` | storage | Coalescing window (ms) for a burst of file changes before a watch-triggered rescan fires; a module dropping many files at once is grouped into one incremental pass (#390, storage v0.6.0). |
| `VAULT_PATH` | `/data/knowledge` | knowledge | Knowledge's path within the shared file space; the on-disk tree is tenant-scoped to `<files-root>/<tenant>/knowledge`, and each top-level folder under it is a project (knowledge base). Was `/vault` before #KB-refactor. |
| `NOTES_ROOT` | `/data/notes` | notes | Notes' path within the shared file space; the on-disk `.md` mirror is tenant-scoped to `<files-root>/<tenant>/notes`. Each saved note is mirrored as `<slug>.md`; Postgres stays the source of truth (#KB-refactor). |

The on-disk file tree is **tenant-scoped** (constraint #1): the three modules build their
roots as `<files-root>/<tenant>/{knowledge,notes}` (and storage serves `<files-root>/<tenant>`),
where `<tenant>` is `DEFAULT_TENANT_ID` (default `local`). The volume mount stays `/data` —
only the in-container path carries the tenant segment.

**Migration note for existing deployments.** `EPICURUS_FILES_ROOT` replaces
`KNOWLEDGE_HOST_VAULT` and `STORAGE_HOST_ROOT`. Move old vault contents into
`<files-root>/<tenant>/knowledge/<project>/` (`<tenant>` = `DEFAULT_TENANT_ID`, default
`local`; each project is a top-level folder) so they appear as knowledge bases.

## Type aliases

```python
Environment = Literal["local", "staging", "production"]
LogLevel = Literal["debug", "info", "warning", "error", "critical"]
```
