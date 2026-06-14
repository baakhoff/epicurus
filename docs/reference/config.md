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
| `llm_default_model` | `LLM_DEFAULT_MODEL` | `str` | `llama3.2` | Model used when a request names none. |
| `llm_keep_alive` | `LLM_KEEP_ALIVE` | `str` | `5m` | How long Ollama keeps a model loaded after use (ADR-0005). |
| `llm_fallbacks` | `LLM_FALLBACKS` | `str` | `""` | Comma-separated fallback models, tried in order (e.g. `claude/claude-3-5-sonnet-latest,gpt/gpt-4o`). |
| `llm_num_retries` | `LLM_NUM_RETRIES` | `int` | `2` | Per-model retries on 429/5xx (LiteLLM backoff). |
| `llm_temperature` | `LLM_TEMPERATURE` | `float \| None` | `None` | Sampling temperature passed to each chat completion (local + hosted). A blank env value means unset. |
| `llm_top_p` | `LLM_TOP_P` | `float \| None` | `None` | Nucleus-sampling `top_p` passed to each chat completion (local + hosted). |
| `llm_num_ctx` | `LLM_NUM_CTX` | `int \| None` | `None` | Ollama context-window size (`num_ctx`); applied to local models only. |
| `module_urls` | `MODULE_URLS` | `str` | `http://echo:8080` | Comma-separated module base URLs; each serves MCP at `<base>/mcp` and its manifest at `<base>/manifest`. |
| `agent_max_steps` | `AGENT_MAX_STEPS` | `int` | `4` | Max tool-calling rounds per agent turn. |
| `database_url` | `DATABASE_URL` | `str` | `postgresql+asyncpg://…/epicurus` | Postgres DSN for conversation persistence. |
| `qdrant_url` | `QDRANT_URL` | `str` | `http://localhost:6333` | Qdrant endpoint for semantic recall. |
| `memory_embed_model` | `MEMORY_EMBED_MODEL` | `str` | `nomic-embed-text` | Local embedding model used for recall. |

### Properties

- **`fallback_models -> list[str]`** — the `llm_fallbacks` chain, parsed.
- **`module_base_urls -> list[str]`** — the `module_urls`, trimmed of a trailing `/`.
- **`module_mcp_urls -> list[str]`** — each module's `<base>/mcp` endpoint.

## Type aliases

```python
Environment = Literal["local", "staging", "production"]
LogLevel = Literal["debug", "info", "warning", "error", "critical"]
```
