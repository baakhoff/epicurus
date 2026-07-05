# Reference: Platform API (`/platform/v1`)

The **platform API** is the module → core HTTP channel (ADR-0004).  A module
calls it to reach core capabilities — inference, secrets, events, storage —
without holding provider credentials or SDK dependencies.  All traffic stays on
the internal Docker network; the API is never exposed externally by default.

Use the typed [`PlatformClient`](#platformclient) from `epicurus_core` rather
than crafting HTTP calls by hand.

> The **file space** endpoints (`/platform/v1/files/*`) — the core-owned, swappable per-tenant
> file store that modules read and write through `PlatformClient.files_*` (ADR-0052), plus the
> core-owned unified **Files** browser / search / read / download
> (`/platform/v1/files/{page,search,read,download}`, ADR-0063) — are documented on their own
> page: [file space](files.md).

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

Once the embedding model is chosen, any **per-model settings** the operator set for it
(context window, keep-alive — `PUT /platform/v1/llm/model-settings`, ADR-0044) are applied as
Ollama runtime options. With nothing set, the embed call is unchanged.

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
Both take an optional `tenant_id` query param, falling back to the default tenant when omitted.

---

## `GET` · `POST` · `DELETE /platform/v1/llm/saved-models`

The operator's saved hosted-model ids (#496) — a tenant-scoped, durable home for the hosted
model strings entered in the chat picker, so they survive restarts / a PWA reinstall and follow
the tenant across devices (unlike the browser's per-origin `recentModels` localStorage cache).

- **`GET`** → `{models: [{model, provider}]}`, most-recently-saved first. `provider` is the id's
  `<provider>/` prefix (for grouping on the Models page).
- **`POST {model}`** persists one id, idempotent (a re-save bumps it to the front). **400**s
  anything that isn't a *hosted* id — a known `<provider>/` prefix (`claude/…`, `gpt/…`, …) — so
  a local `hf.co/org/model:tag` can never land here (the server-side half of the fix for the web
  client's old `includes("/")` misclassification).
- **`DELETE ?model=…`** (query param, since ids carry `/` and `:`) forgets one; a no-op if absent.

Backed by the tenant-scoped `saved_models` table. The chat picker renders this list (auto-saving
on use), the Models page lists it (remove / set-as-default), and module model slots offer it
(ADR-0029). Mutations **503** when the store is unavailable.

## `GET /platform/v1/agent/instructions` · `PUT /platform/v1/agent/instructions`

The agent's editable **base system prompt** (#497, ADR-0083) — injected as the **first** message
of every turn (chat and headless bridge turns alike), ahead of recalled memory and attached
context, where the compaction leading-prefix rule protects it from being trimmed. `GET` returns
`{instructions, is_default}`: the effective prompt (the stored value, else the shipped default)
and whether it is the default (so the editor can offer *Reset to default*). `PUT {instructions}`
sets it; a `null`/blank body **resets** to the default, and a prompt over **32,000 characters**
is rejected with a `422` (the prompt leads every turn and is never compacted away, so a runaway
value must not be storable). Both take an optional `tenant_id`. Resolved per turn, so an edit
takes effect on the next message with no restart. Edited in the web
**Settings → Assistant instructions** card.

---

## `GET /platform/v1/maintenance` · `POST /platform/v1/maintenance/run`

The maintenance orchestrator (ADR-0060) — one coordinated batch over the core's background jobs
(memory fact-extraction drain, module re-index fan-out). `GET` returns
`{schedule_enabled, schedule_hour, jobs:[{key,label,nightly}], last_run}` — the registered jobs,
the opt-in nightly schedule, and the last run (or `null`). `POST /run` runs **every** job now
(``scope: "all"``) and returns the `MaintenanceRun` `{ran_at, scope, jobs:[{key,label,status,detail}]}`
— `status` is `ok`/`skipped`/`error` per job (one job's failure never aborts the rest). A
tenant-scoped `maintenance.completed` NATS event carries the same summary. Driven by the web
**Settings → Maintenance** card. The nightly schedule runs only `nightly` jobs and is off unless
`MAINTENANCE_SCHEDULE_ENABLED` is set.

---

## Knowledge-base / notes / suggestions endpoints (shell-facing)

These are consumed by the web shell, not the `PlatformClient`. The full module-registry
surface is documented in [core-app](../services/core-app.md); the #KB-refactor additions are
(the suggestion endpoints below are generic — they serve any module with a `review` page,
i.e. knowledge **and** private notes):

### `GET /platform/v1/suggestions`

The **cross-module pending-suggestions feed**: every enabled module that declares a `review`
page, aggregated into one list. Each item is a review suggestion plus its owning `module` and
`page_id`, so the chat composer's suggestion bubble and the Suggestions page can act on it
from anywhere. This spans **all** such modules — the knowledge base (`module: "knowledge"`,
`page_id: "review"`) and private **notes** (`module: "notes"`, `page_id: "review"`) both
surface here, with no special-casing. Best-effort — a down, disabled, or erroring module is
skipped, not fatal.

```json
[
  {
    "id": "9f2c…",
    "title": "goals",
    "path": "projects/goals.md",
    "operation": "update",
    "origin": "agent",
    "note": "",
    "created_at": "2026-06-24T10:00:00+00:00",
    "diff": "--- a/projects/goals.md\n+++ b/projects/goals.md\n…",
    "to_path": "",
    "current": "…",
    "content": "…",
    "module": "knowledge",
    "page_id": "review"
  }
]
```

`operation` is one of `create` / `update` / `append` / `delete` / `move` / `mkdir` /
`mkproject`. The content ops (`create` / `update` / `append`) carry a `diff` and full
`current` / `content` for per-hunk review; structural ops (`move` / `mkdir` / `mkproject`)
leave those empty, with `to_path` carrying a `move`'s destination. `append` is **notes**-only
(the agent supplies just the text to add; the server concatenates it on approval) and is
content-like — its diff shows the added text.

### `GET` · `PUT /platform/v1/modules/{name}/suggestions-enabled`

The per-module **review on/off** toggle (#KB-refactor) — `{ "enabled": true }`. When **on**
(the default; a missing or NULL pref reads as `true`) the module stages agent changes on its
`review` page for approval. When **off**, the module applies them directly (it reads this via
`PlatformClient.get_suggestions_enabled` and, when off, approves its own staged suggestion
through the normal apply path). The review-page header reads `GET` and writes `PUT`; `PUT`
**404**s an unknown module. Persisted in `module_prefs`. Generic across any module with a
`review` page — today knowledge and notes.

### Files read / download moved to the core (ADR-0063)

The unified **Files** read and download are now **core-owned** at `GET /platform/v1/files/read`
and `GET /platform/v1/files/download` — the core reads the file space first and **falls back to
the storage object store** (`GET /objects/read` / `GET /download` on the storage module) for
object entries (chat uploads, agent-written files). The former storage filesystem read proxy
(`GET /platform/v1/modules/storage/read`) is **removed**; see [file space](files.md). The generic
`GET /platform/v1/modules/{name}/read` proxy still serves an **editor** module's split-screen
reader (knowledge, notes).

### `POST /platform/v1/modules/{name}/pages/{page_id}/project?project=…`

Create a new knowledge base (project / top-level scope) in an editor page's store →
`{id, title, kind}`. **409** if it already exists, **400** for an invalid name (a single
folder segment — no separators, `..`, or `.`/`_` prefix). The operator's "New knowledge base"
control; the agent's equivalent (`knowledge_propose_project`) goes through the review queue.

### `POST /platform/v1/modules/{name}/pages/{page_id}/suggestions/{id}/approve`

Approve a staged suggestion: the module applies + indexes it (ADR-0033). Generic across any
module that declares a `review` page — both **knowledge** (`page_id: "review"`) and private
**notes** (`page_id: "review"`) expose this surface. The body is **optional** `{content}` — the
operator's **per-hunk-merged** result for a content op (an edit, or a notes `append`), forwarded
so only the approved changes are written; absent ⇒ apply the module's full proposal (for notes,
the server composes the body — `append` concatenates onto the current note). Operator-only
(paired with `…/reject`, which discards). **409** for knowledge when the target vault is
externally owned (watch mode, #232); notes have no external-owner mode.

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
