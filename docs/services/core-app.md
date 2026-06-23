# core-app — the core runtime

**`epicurus-core-app`** is the brain of the platform — the one service everything else
builds on (ADR-0009). It hosts the **agent loop**, the **LLM gateway**, **cross-chat
memory**, the **power-state machine**, the **module registry**, and the **MCP host**, and
it serves the module- and UI-facing **platform API**. Unlike a sidecar module (which
exposes MCP tools *to* the agent), core-app is the **host**: it is the agent that calls
modules, and the platform other services depend on.

Built on the [`epicurus-core`](../reference/index.md) library. Host port **8082**;
reachable through the edge gateway at `core-app.localhost`.

## The contract it exposes

Everything lives under **`/platform/v1`** (the module → core platform API, ADR-0004),
plus the shared ops endpoints. All of it is internal/local-only by default.

### Ops

| Method · Path | Purpose |
| --- | --- |
| `GET /health` | Liveness + service name + version. |
| `GET /metrics` | Prometheus metrics. |
| `GET /platform/v1/info` | Discovery: contract version, core version, tenant. |

### Inference (module-facing — used by the `PlatformClient`)

| Method · Path | Purpose |
| --- | --- |
| `POST /platform/v1/embed` | Embed texts (returns float vectors). Resolution order: per-module override → global embed default pref → `MEMORY_EMBED_MODEL`. |
| `POST /platform/v1/chat` | Chat completion — **the single module-facing chat path** (ADR-0021). Module supplies messages; the core owns model/keys/fallback. Returns the shared `ChatResult`. |

Modules never hold model keys — all AI goes through here (ADR-0010). See
[platform-client](../reference/platform-client.md).

### Agent (ADR-0001)

| Method · Path | Purpose |
| --- | --- |
| `POST /platform/v1/agent/chat` | Run one turn (offer module tools → run tool calls over MCP → loop to an answer, `AGENT_MAX_STEPS` rounds). Returns `AgentTurn`. |
| `POST /platform/v1/agent/chat/stream` | The same turn as **SSE**: an optional leading `readiness` (warming progress, ADR-0027) · `delta` (answer tokens) · `thinking` (chain-of-thought tokens, ADR-0041) · `tool` (a tool ran) · `done` (final turn) · `error`. The web shell speaks this. |
| `GET /platform/v1/agent/sessions` | List conversations (title + last-active + count). |
| `GET /platform/v1/agent/sessions/{id}` | A session's full transcript. |
| `DELETE /platform/v1/agent/sessions/{id}` | Forget a session (rows + recall vectors). |
| `POST /platform/v1/agent/sessions/{id}/regenerate` | Re-answer the session's last user turn, dropping the previous answer. Body `{model?}`. Truncates everything after the last user message (history + recall), then streams a fresh turn — same SSE protocol as `/chat/stream`; an `error` event if there's no user turn (#302). |
| `POST /platform/v1/agent/sessions/{id}/edit` | Replace the last user message with `{content}` (and `{model?}`) and re-answer it in place — edits, re-indexes recall, truncates the tail, then streams. An `error` event on empty content or no user turn (#302). |
| `GET /platform/v1/agent/memory?q=&limit=` | The cross-chat recall corpus — what the model remembers and pulls into future chats. No `q`: the corpus newest-first; with `q`: what recall surfaces for that query (the same ranking a turn gets). Returns `{items, total}` — each `MemoryItem` carries role + timestamp (joined from `agent_messages`) and, for a search, a match `score`. `limit` is bounded 1–500 (default 100). Backs the **Memory** screen (ADR-0040). |
| `DELETE /platform/v1/agent/memory/{point_id}` | Forget one remembered snippet so it stops being recalled. Drops the recall **vector only** — the source message stays in its conversation. Returns `{forgotten}`. |
| `POST /platform/v1/agent/attachments` | Upload a file to attach to a turn → its core-side handle (`att_id`). Capped at `ATTACHMENT_MAX_BYTES` (10 MiB; **413** over) with a content-type allowlist (`ATTACHMENT_ALLOWED_TYPES`; **415** if disallowed); best-effort mirrored to the storage sink (ADR-0025). |

Passing a `session_id` opts a turn into cross-chat memory (below).

### Built-in agent tools (ADR-0039)

Besides the modules' MCP tools, the core offers **built-in tools** the agent can call,
dispatched in-process (no module round-trip). They're registered on the `McpHost`
(`register_builtin`) and routed via a `"__builtin__"` sentinel; they respect the same
per-tool disable filter as module tools.

- **`now(timezone?)`** — the current date/time. The agent has no inherent clock, so it
  calls this for anything date/time-relative ("tomorrow", "at 19:00"). Returns the time
  in the operator's configured timezone (or the `timezone` argument) plus UTC and the
  weekday; when a connected calendar uses a *different* timezone, that is reported with a
  note so events land in the intended zone. The configured timezone is read from:

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/timezone` | The operator's effective IANA timezone (stored value, else `DEFAULT_TIMEZONE`). |
| `PUT /platform/v1/timezone` | Set the timezone (`{timezone}`; validated as a real IANA zone, **400** otherwise). Edited in the web **Settings → Timezone** card. |

### LLM gateway (ADR-0010)

The gateway's HTTP surface is **model/provider management** (consumed by the web UI).
Chat completions go through `POST /platform/v1/chat` above (ADR-0021); the gateway's
own `POST /platform/v1/llm/chat` was **removed in `core-app` 0.2.0** — it duplicated
`/chat` (which is a strict superset: it also accepts `tools` + `tenant_id`).

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/llm/models` · `DELETE /platform/v1/llm/models?name=…` | List / remove local models (the `loaded` flag marks in-memory ones). |
| `GET /platform/v1/llm/catalog` | The browsable model catalog the core parses from upstream on a schedule (#269). Returns `{entries[], source, updated_at, stale}`; `stale` flags a seed / last-good list served after a failed or skipped refresh. See **Model catalog** below. |
| `POST /platform/v1/llm/pull` · `POST /platform/v1/llm/pull/stream` | Pull a model (blocking / SSE progress). |
| `GET /platform/v1/llm/providers` | Providers and whether each one's key is set. |
| `PUT` · `DELETE /platform/v1/llm/providers/{alias}/key` | Store / clear a hosted provider's key (core → OpenBao; never logged or returned). |
| `GET /platform/v1/llm/prefs` | Stored preferences: `global_default` (chat), `global_embed_default` (embedding), `hidden` (model list). |
| `PUT /platform/v1/llm/prefs/default` | Set or clear the global default chat model (`{model: str|null}`). |
| `PUT /platform/v1/llm/prefs/embed-default` | Set or clear the global default embedding model (`{model: str|null}`). Modules with no per-module override use this; per-module selections win (#214). |
| `PUT /platform/v1/llm/prefs/hidden` | Toggle a model's hidden state (`{name, hidden}`). |

#### Model catalog (#269)

The model browser's "Browse models" list is parsed by the core, not hand-maintained in
the web build. A `ModelCatalog` (`llm/catalog.py`) fetches a configurable source
(`LLM_CATALOG_URL`, the public Ollama library by default), parses each model's sizes,
description, capabilities (→ the browser's tag vocabulary) and popularity into
`CatalogEntry` rows (one per pullable size), caches the snapshot, and **refreshes it on a
background loop** (`LLM_CATALOG_REFRESH_SECONDS`, default 6h). `GET …/llm/catalog` returns
the cached snapshot — it never blocks on the network.

It degrades gracefully: a failed or empty parse keeps the last-good snapshot and flags it
`stale`; before any successful fetch (cold start, or an air-gapped build with
`LLM_CATALOG_ENABLED=false`) it serves a small built-in **seed**, so the browser is never
empty. The catalog is **global, not tenant-scoped** — it mirrors a public registry, holds
no tenant data, and is identical for every tenant (like the provider registry). The web
shell falls back to its own bundled list only if this endpoint is unreachable (e.g. an
older core).

### Power (ADR-0005)

| Method · Path | Purpose |
| --- | --- |
| `GET` · `PUT /platform/v1/power` | The main-page power toggle: `paused` unloads models and refuses local inference (`503`); `idle` resumes. |

### Readiness (ADR-0027)

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/readiness?model=…` | A warming snapshot — `{ready, power, components[]}` — folding the power state, module health (compose health), and whether the turn's model is warm (hosted models are always ready). Best-effort: a slow/failing component reports not-yet-ready rather than erroring. The chat stream emits the **same** snapshot as leading `readiness` events so the UI shows a progress bar before the first token. |

### Module registry (ADR-0004/0007)

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/modules` | Every configured module: its manifest (tools, events, declared UI), live health, and the operator's `enabled` flag (#126). Disabled modules stay listed so the shell can re-enable them. |
| `GET` · `PUT /platform/v1/modules/{name}/config` | The module's config values (stored tenant-scoped in OpenBao at `modules/<name>/config`). |
| `POST /platform/v1/modules/{name}/enabled` | Enable/disable a module (#126): `{enabled: bool}`. Hides its tools, pages, and actions from the agent and shell while the container keeps running. Persisted in Postgres (`module_prefs`). |
| `DELETE /platform/v1/modules/{name}` | **Privileged** confirmed removal (#127, ADR-0028): stop + remove the module's container via the Docker socket, then tombstone it. Refuses core-app / web / data-plane, scoped to the core's own Compose project. **403** protected · **503** no Docker access · **404** unknown. |
| `GET` · `PUT /platform/v1/modules/{name}/models` | Per-module model-slot selections (#128, ADR-0029): `{slot_key: model_id}`. `PUT` validates each key against the manifest's `required_models` (**400** otherwise). Persisted in Postgres (`module_prefs`). |
| `GET /platform/v1/modules/{name}/models/{slot}` | Resolve one slot to its chosen model (`null` = core default) — backs `PlatformClient.get_module_model` (#128). |
| `GET /platform/v1/modules/{name}/collections` | The module's connected accounts + collections (ADR-0030), proxied from its `GET /accounts` and **merged** with the operator's stored selection (each collection annotated `enabled`/`active`). **404** if the module declares no `collections`. |
| `PUT /platform/v1/modules/{name}/collections` | Persist the selection: `{enabled: [CollectionRef], active: CollectionRef \| null}`. Store-through (refs are not live-validated); `active` must be in `enabled` (**400** otherwise). Persisted in Postgres (`module_prefs`). |
| `GET /platform/v1/modules/{name}/collections/prefs` | The raw stored `{enabled, active}` (Postgres only, no module round-trip) — backs `PlatformClient.get_collections` so a module resolves its own routing (ADR-0030). |
| `POST /platform/v1/modules/{name}/tools/{tool}/enabled` | Enable or disable one tool (#213): `{enabled: bool}`. Hides the named tool from the agent while the module keeps running and other tools remain unaffected. **404** unknown module or undeclared tool. Persisted in Postgres (`module_prefs`). |
| `POST /platform/v1/modules/{name}/tools/{tool}` | Invoke a manifest-declared UI action (runs the module's MCP tool through the host). **403** if the module is disabled. |
| `GET /platform/v1/modules/{name}/status` | Proxy the module's `ui.status_url` endpoint (returns the module's live status JSON as-is). 404 if the module is unreachable or has no `status_url`. |

> **Privileged surface (ADR-0028).** Module removal needs the Docker socket, mounted
> read-write on `core-app` **only**. The core touches it through a single `DockerController`
> that stops/removes **only a configured module's own container** — scoped to this Compose
> project, and never core-app / web / a data-plane service. Drop the socket mount to disable
> removal entirely (the endpoint then returns `503`).

Caller-supplied path segments the registry interpolates into a module request —
`ref_id`, entity `kind`, `page_id` — reject `/`, `\`, or `..` with **400** so a
supplied id cannot redirect the outbound request on the module host (#175).

Every module-proxy GET (status, docs, pages, resolve, attachments, accounts) maps an
upstream failure to a **controlled** status, not an unhandled exception (#209): a module's
client error (4xx) passes through as-is (e.g. a missing entity stays a `404`), while a 5xx,
a timeout, or a connection failure becomes a `502` carrying the operation — so a slow or
erroring module can no longer surface as an opaque **Bad Gateway** to the shell.

### Events (NATS)

Emits **`<tenant>.llm.usage`** after every inference call — model, token counts, latency.
No prompt/response content, no keys. Feeds observability now and SaaS metering later.

## Configuration

`CoreAppSettings` extends the shared [`CoreSettings`](../reference/config.md). Key fields
(full table in the [config reference](../reference/config.md#coreappsettings)):

| Env var | Default | Meaning |
| --- | --- | --- |
| `OLLAMA_URL` | `http://ollama:11434` | Local LLM runtime. |
| `LLM_DEFAULT_MODEL` | `llama3.2` | Model when a request names none. |
| `LLM_FALLBACKS` | — | Comma-separated fallback chain (e.g. `claude/claude-3-5-sonnet-latest`). |
| `LLM_KEEP_ALIVE` | `5m` | How long Ollama keeps a model loaded (ADR-0005). |
| `LLM_TEMPERATURE` | — | Sampling temperature (local + hosted); blank = provider default. |
| `LLM_TOP_P` | — | Nucleus-sampling `top_p` (local + hosted). |
| `LLM_NUM_CTX` | — | Ollama context window (`num_ctx`); local models only. |
| `MODULE_URLS` | `http://echo:8080,…` | Module base URLs the host discovers tools from. |
| `AGENT_MAX_STEPS` | `4` | Max tool-calling rounds per turn. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Conversation persistence. |
| `QDRANT_URL` | `http://qdrant:6333` | Semantic-recall vectors. |
| `MEMORY_EMBED_MODEL` | `nomic-embed-text` | Local embedding model for recall. |
| `DEFAULT_TIMEZONE` | `UTC` | Fallback IANA timezone for the `now` tool when unset in Settings (ADR-0039). |

Provider keys are **not** configured here — they go through the UI into OpenBao.

## Data model

- **Postgres `agent_messages`** — conversation history (append-only in normal use; the last
  turn can be edited/truncated for regenerate/edit, #302): `id`, `tenant`,
  `session_id`, `role`, `content`, `created_at`, plus JSON `entity_refs` / `attachments`
  (ADR-0019) and `activity` — the assistant turn's persisted process, rendered as the folded
  activity timeline on reopen (ADR-0041). `activity.timeline` is the **chronological**
  interleaving of thinking blocks and tool steps (think → call → think, #300); the flat
  `thinking`/`steps` are derived and kept for backward compatibility (older rows have only
  those). Tenant-scoped; post-release columns are added in place at startup (no migration).
- **Postgres `llm_prefs`** — per-tenant operator preferences: `global_default` (chat model),
  `global_embed_default` (embedding model, #214), `hidden_models` (JSON list). A missing row
  means all defaults are `null` (fall back to env settings).
- **Postgres `module_prefs`** — per-`(tenant, module)` operator preferences: `enabled`
  holds the enable/disable flag (#126), `removed` tombstones a module after its container is
  deleted (#127), `models` holds per-slot model choices (#128), `disabled_tools` holds a JSON
  list of tool names the operator has toggled off (#213), and `collections` holds the
  account/collection selection (`{enabled, active}` JSON, ADR-0030). A module with no row
  defaults to enabled, not-removed, core-default models, all tools on, and the local default
  collection. Post-release columns are added in place at startup (no migration framework).
- **Postgres `timezone_prefs`** — per-tenant IANA timezone for the `now` tool (ADR-0039):
  `tenant`, `timezone`. A missing row (or null) falls back to `DEFAULT_TIMEZONE`.
- **Qdrant `<tenant>__memory`** — embeddings of past turns for cross-chat semantic recall
  (768-dim, cosine), one collection per tenant. Each point's id **is** the source
  `agent_messages.id`, and its payload carries `{session_id, text}`. The **Memory** view
  (ADR-0040) lists and searches this collection and joins the ids back to `agent_messages`
  for each snippet's role + timestamp; forgetting one memory deletes its vector here and
  leaves the message row intact.

Memory is **best-effort**: if Postgres, Qdrant, or the embedder is down, a turn still
answers — just without memory — and never blocks core startup.

## Dependencies

Ollama (models) · Postgres (memory) · Qdrant (recall) · OpenBao (provider + module
secrets) · NATS (usage events) · the modules in `MODULE_URLS` (tools, over MCP).

## Run & extend

```bash
docker compose up -d core-app      # comes up with the full stack
```

Source is one package, `epicurus_core_app`, split by responsibility: `agent/`
(loop + MCP host + routes), `llm/` (gateway, providers, power, models), `memory/`
(store + recall + facade), `modules.py` (registry), `platform_api.py` (inference
endpoints), `app.py` (wiring). The agent targets only the gateway's interface and
modules only through MCP — never a provider SDK.
