# Reference: Platform API (`/platform/v1`)

The **platform API** is the module → core HTTP channel (ADR-0004).  A module
calls it to reach core capabilities — inference, secrets, events, storage —
without holding provider credentials or SDK dependencies.  All traffic stays on
the internal Docker network; the API is never exposed externally by default.

Use the typed [`PlatformClient`](#platformclient) from `epicurus_core` rather
than crafting HTTP calls by hand.

---

## `GET /platform/v1/info`

Discovery — what core version and contract are running.

**Response**

```json
{
  "contract_version": "0.1",
  "core_version": "0.2.0",
  "tenant": "local"
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `contract_version` | `str` | The module↔core contract version (see `CONTRACT_VERSION`). |
| `core_version` | `str` | The installed `epicurus-core-app` version. |
| `tenant` | `str` | The active tenant ID. |

---

## `POST /platform/v1/embed`

Embed one or more texts via the core's LLM gateway.  The core resolves the
embedding model using this priority chain and emits a usage event on NATS.
No provider key ever leaves the core.

**Embedding model resolution order**

1. `model` in the request body (per-module override — the module passes the value
   from its `required_models` slot via `PlatformClient.get_module_model`).
2. Tenant's `global_embed_default` pref (set via `PUT /platform/v1/llm/prefs/embed-default`,
   persisted in `llm_prefs`; #214).
3. `MEMORY_EMBED_MODEL` env setting (`nomic-embed-text` by default).

**Request body**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `texts` | `list[str]` | Yes | Texts to embed.  One vector returned per item. |
| `model` | `str \| null` | No | Per-module override.  Omit to use the global embed default or env default. |
| `tenant_id` | `str \| null` | No | Tenant scope.  Defaults to the core's configured tenant. |

**Response**

```json
{
  "embeddings": [
    [0.023, -0.117, ...],
    [0.089,  0.042, ...]
  ]
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `embeddings` | `list[list[float]]` | One float vector per input text, in order. |

**Error responses**

| Status | Condition |
| --- | --- |
| 503 | Gateway is paused (ADR-0005) — resume to run local inference. |

---

## `POST /platform/v1/chat`

Chat completion via the core's LLM gateway.  The core owns model routing,
fallback, key management, and usage accounting.  This is the **single
module-facing chat path** (ADR-0021); the response is the shared `ChatResult`
model.  (The gateway's former `POST /platform/v1/llm/chat` was removed in
`core-app` 0.2.0 — it duplicated this endpoint.)

**Request body**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `messages` | `list[object]` | Yes | Conversation history.  Each item is a `ChatMessage`-shaped object (`role`, `content`, optional `tool_calls` / `tool_call_id` / `name`). |
| `model` | `str \| null` | No | Override the model (e.g. `"claude/claude-3-5-sonnet-latest"`).  Omit to use the core default and fallback chain. |
| `tools` | `list[object] \| null` | No | OpenAI-format tool descriptors for function calling. |
| `tenant_id` | `str \| null` | No | Tenant scope.  Defaults to the core's configured tenant. |

**Response**

```json
{
  "model": "ollama_chat/llama3.2",
  "content": "Here is your answer …",
  "tool_calls": null,
  "prompt_tokens": 42,
  "completion_tokens": 17
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `model` | `str` | The model that produced the completion. |
| `content` | `str` | The assistant reply text. |
| `tool_calls` | `list[object] \| null` | Tool-call requests from the model, or `null`. |
| `prompt_tokens` | `int \| null` | Input token count (when reported by the provider). |
| `completion_tokens` | `int \| null` | Output token count (when reported by the provider). |

**Error responses**

| Status | Condition |
| --- | --- |
| 503 | Gateway is paused with no hosted fallback available. |

## `GET /platform/v1/timezone` · `PUT /platform/v1/timezone`

The operator's IANA timezone, used by the agent's built-in `now` tool (ADR-0039). `GET`
returns `{timezone}` (the stored value, else `DEFAULT_TIMEZONE`). `PUT {timezone}` validates
it as a real IANA zone (**400** otherwise) and persists it; edited in the web Settings screen.

---

## `PlatformClient`

`epicurus_core.PlatformClient` — the typed client for the above endpoints.
Instantiate one per module service, scoped to the tenant.

```python
from epicurus_core import PlatformClient, PlatformMessage

client = PlatformClient(
    base_url="http://core:8080",   # PLATFORM_URL env var in the service template
    tenant_id="local",             # settings.default_tenant_id
)
```

### `PlatformClient(base_url, tenant_id)`

| Param | Type | Meaning |
| --- | --- | --- |
| `base_url` | `str` | Internal base URL of the core service. |
| `tenant_id` | `str` | Tenant this module acts on behalf of. |

### `await client.embed(texts, *, model=None) → list[list[float]]`

Embed *texts* and return one float vector per item.

| Param | Type | Meaning |
| --- | --- | --- |
| `texts` | `list[str]` | Texts to embed. |
| `model` | `str \| None` | Override embedding model (omit for core default). |

Raises `httpx.HTTPStatusError` on non-2xx (e.g. 503 when paused).

### `await client.chat(messages, *, model=None, tools=None) → PlatformChatResponse`

Chat completion.

| Param | Type | Meaning |
| --- | --- | --- |
| `messages` | `list[PlatformMessage]` | Conversation history. |
| `model` | `str \| None` | Model override. |
| `tools` | `list[dict] \| None` | Tool descriptors for function calling. |

Raises `httpx.HTTPStatusError` on non-2xx.

### `PlatformMessage` and `PlatformChatResponse`

Both are the **shared chat contract** (ADR-0021): `PlatformMessage` is an alias of
`ChatMessage` and `PlatformChatResponse` of `ChatResult` (both exported from
`epicurus_core`). The `Platform*` names are retained for backward compatibility, so
there is a single definition of each shape.

```python
class PlatformMessage(BaseModel):
    role: str                           # "system" | "user" | "assistant" | "tool"
    content: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None
```

### `PlatformChatResponse`

```python
class PlatformChatResponse(BaseModel):
    model: str
    content: str
    tool_calls: list[dict] | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
```

---

## OAuth token endpoint (module-facing)

Modules that need a Google (or other provider) access token call:

```
GET /platform/v1/oauth/{provider}/token?tenant_id={tenant}
```

The core returns a valid, auto-refreshed access token — the module never touches the client secret or refresh flow.  Full reference: [OAuth 2.0](oauth.md).
