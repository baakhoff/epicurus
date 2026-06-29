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
| `POST /platform/v1/agent/chat` | Run one turn (offer module tools → run tool calls over MCP → loop to an answer). The round bound is resolved **per turn** from the operator's stored pref, else the `AGENT_MAX_STEPS` env default (#297). Returns `AgentTurn`. |
| `POST /platform/v1/agent/chat/stream` | The same turn as **SSE**: an optional leading `readiness` (warming progress, ADR-0027) · `delta` (answer tokens) · `thinking` (chain-of-thought tokens, ADR-0041) · `tool` (a tool ran) · `awaiting_input` (the turn paused on `ask_user` — carries `{run_id, question}`, ADR-0053) · `done` (final turn) · `error`. Each data frame carries an `id:` (a live-run seq) for re-attach. The turn runs **decoupled from this connection** (ADR-0055): a disconnect doesn't abort it — the answer still persists and the client re-attaches. A turn already running for the session yields **409** (+ `X-Run-Id`). The web shell speaks this. |
| `GET /platform/v1/agent/sessions` | List conversations (title + last-active + count). |
| `GET /platform/v1/agent/sessions/{id}` | A session's full transcript. |
| `GET /platform/v1/agent/sessions/{id}/active-run` | The session's in-flight run to re-attach to — `{run_id, last_seq}` or `null` if none is live (ADR-0055). How a client rediscovers a turn after a reload/reconnect. |
| `DELETE /platform/v1/agent/sessions/{id}/active-run` | Cancel the session's in-flight turn — the explicit **Stop** (a decoupled turn outlives the connection, so Stop must say so). Returns `{cancelled}` (ADR-0055). |
| `GET /platform/v1/agent/active-runs` | Session ids with an in-flight turn right now — `{session_ids}`. Drives the conversations-list running indicator (#396) in one request rather than polling each row; tenant-scoped, best-effort/point-in-time (the live-run buffer is a disposable cache). |
| `DELETE /platform/v1/agent/sessions/{id}` | Forget a conversation — its history rows. Facts the user is remembered by are kept (ADR-0045). |
| `POST /platform/v1/agent/sessions/{id}/regenerate` | Re-answer the session's last user turn, dropping the previous answer. Body `{model?}`. Truncates everything after the last user message, then streams a fresh turn — same SSE protocol as `/chat/stream`; an `error` event if there's no user turn (#302). |
| `POST /platform/v1/agent/sessions/{id}/edit` | Replace the last user message with `{content}` (and `{model?}`) and re-answer it in place — edits the message, truncates the tail, then streams. An `error` event on empty content or no user turn (#302). |
| `POST /platform/v1/agent/runs/{run_id}/resume` | Resume a turn paused by `ask_user`, supplying `{answer}` (ADR-0053). Consumes the suspended run, appends the answer as the pending tool call's result, and continues the same turn — same SSE protocol as `/chat/stream`. An `error` event if the run is unknown / expired / already answered. |
| `GET /platform/v1/agent/runs/{run_id}/stream?after_seq=N` | **Re-attach** to an in-flight turn (ADR-0055), replaying buffered events after `N` (or `Last-Event-ID`) then tailing live — same SSE protocol as `/chat/stream`, no readiness prelude. A `gone` event if the run is unknown / finished-and-reaped (the client then falls back to the durable transcript). Note: this `run_id` is a **live-run** id (in-memory, for re-attach), distinct from the suspended-run id used by `/resume`. |
| `GET /platform/v1/agent/memory?q=&limit=` | The cross-chat memory corpus — the durable **facts** the model remembers about the user (ADR-0045). No `q`: the facts newest-first; with `q`: what recall surfaces for that query (the same ranking a turn gets). Returns `{items, total}` — each `MemoryItem` is `{id, text, source, created_at?, score?}` where `source` is `tool` (the `remember` tool) or `auto` (background extraction); `score` is set only for a search. `limit` is bounded 1–500 (default 200). Backs the **Settings → Memory** box. |
| `DELETE /platform/v1/agent/memory/{id}` | Forget one remembered fact so it stops being recalled. Drops its vector; the conversation that surfaced it is untouched. Returns `{forgotten}`. |
| `POST /platform/v1/agent/attachments` | Upload a file to attach to a turn → its core-side handle (`att_id`). Capped at `ATTACHMENT_MAX_BYTES` (10 MiB; **413** over) with a content-type allowlist (`ATTACHMENT_ALLOWED_TYPES`; **415** if disallowed); best-effort mirrored to the storage sink (ADR-0025). |

Tools are offered to the model **only when it can use them**: the loop checks the resolved
model's capabilities (`gateway.supports_tools` → `/api/show`; hosted providers are assumed
capable) and, for a tool-less local model, calls without tools so the turn falls back to a
plain text answer instead of the runtime erroring. The web shell surfaces the same fact as a
"can't use tools" hint in the composer.

A turn **never ends silently empty.** A reasoning model sometimes emits its `<think>` block and
then stops — no answer text, no tool call — which would persist as an empty turn and render as a
silent "stop". The loop nudges such a step once to commit to an answer, then (if it still says
nothing, even on the forced final round) substitutes a clear fallback message and logs `turn
produced no answer; using fallback` with whether the model reasoned and whether it was nudged.

Passing a `session_id` opts a turn into cross-chat memory (below).

**Durable, re-attachable turns (ADR-0055).** A streamed turn runs in a **detached task** (the
`LiveRunRegistry` in `agent/live_runs.py`), not inline in the request — so a client disconnect
(a mobile PWA backgrounded, a hard refresh, a network blip) ends only the HTTP *subscriber*,
never the turn. The task drives `run_stream` into a seq-tagged in-memory buffer and persists the
answer to `agent_messages` regardless of who is listening (the answer write is `asyncio.shield`-ed
so even a shutdown flushes a finished answer). A subscriber replays that buffer then tails live
events; a reconnecting client rediscovers its run via `…/active-run` and re-attaches via
`…/runs/{id}/stream` (replay from its last seq), or — if the turn finished while it was away —
reads the now-durable transcript. The buffer is **disposable cache**, not authoritative state
(constraint #2): on any miss (unknown/reaped run, server restart, a different instance) the
client falls back to history. Finished runs are reaped after `LIVE_RUN_GRACE_SECONDS`. At most
one *running* run exists per `(tenant, session)` — a second start gets `409` (+ `X-Run-Id`).
Multi-instance re-attach (a shared event log over Valkey/NATS, or sticky routing) is a named
follow-up; v1 is single-instance.

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

- **`remember(fact)`** — save a durable fact about the user to long-term memory (ADR-0045).
  The agent's explicit, *hot-path* way to remember: it calls this when the user says
  "remember…" or it learns a stable detail/preference. The fact is written to the user-fact
  store (`source=tool`) for the **calling tenant** — built-in handlers receive the tenant
  precisely so `remember` can scope its write. A near-duplicate of an existing fact is a
  no-op. The *implicit* path is background extraction — deferred to a nightly drain by default
  (ADR-0051; see **Data model**); together they are the corpus that recall pulls into later chats.
- **`ask_user(question)`** — pause the turn to ask the operator a clarifying question
  (ADR-0053). Unlike other built-ins it is **not executed inline**: the agent loop intercepts
  the call, persists the in-progress run (`agent_suspended_runs`), emits an `awaiting_input`
  SSE event, and ends the stream. The web shows the question + an input; the answer is POSTed
  to `…/agent/runs/{run_id}/resume`, which rehydrates the run and continues the same turn with
  the answer as the tool result. The suspended run is consumed on resume and reaped after
  `ASK_USER_TTL_HOURS`. With no suspend store wired the loop degrades — the model is told to
  proceed with its best assumption rather than pausing.

### LLM gateway (ADR-0010)

The gateway's HTTP surface is **model/provider management** (consumed by the web UI).
Chat completions go through `POST /platform/v1/chat` above (ADR-0021); the gateway's
own `POST /platform/v1/llm/chat` was **removed in `core-app` 0.2.0** — it duplicated
`/chat` (which is a strict superset: it also accepts `tools` + `tenant_id`).

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/llm/models[?capabilities=true]` · `DELETE /platform/v1/llm/models?name=…` | List / remove local models (the `loaded` flag marks in-memory ones). `?capabilities=true` additionally fills each model's reported `capabilities` (e.g. `tools`, `vision`) from `/api/show` — opt-in (one call per model), so the Models page can badge them while the chat picker stays light. |
| `GET /platform/v1/llm/models/details?model=…` | Read-only facts about a local model from the runtime's `/api/show`: `{quantization, parameter_size, context_length, family, capabilities}` (any field `null`/empty when not reported). Backs the model-settings sheet and the chat "can't use tools" hint. `model` is a query param (names carry `:`/`/`). |
| `GET /platform/v1/llm/catalog` | The browsable model catalog the core parses from upstream on a schedule (#269). Returns `{entries[], source, updated_at, stale}`; `stale` flags a seed / last-good list served after a failed or skipped refresh. See **Model catalog** below. |
| `GET /platform/v1/llm/catalog/variants?model=…` | The quant variants available for a model (#330), looked up on demand from the model's public library **tags page** (the catalog index lists *sizes*, not quants). Returns `{model, variants:[{tag, quant}]}`; best-effort — an empty list (offline, or a model not in the public library) makes the UI fall back to a manual tag box. `model` is a query param. See **Model catalog** below. |
| `POST /platform/v1/llm/pull` · `POST /platform/v1/llm/pull/stream` | Pull a model (blocking / SSE progress). |
| `POST /platform/v1/llm/unload` | Drop model(s) from memory now (`keep_alive=0`) **without** changing power state (#331). Body `{model: str\|null}` — `null`/omitted unloads every loaded model, a name unloads just that one. Returns `{status, model}` (`"all"` when none given). The standalone unload the Models page calls; the `loaded` flag refreshes on the next poll. |
| `GET /platform/v1/llm/providers` | Providers and whether each one's key is set. |
| `PUT` · `DELETE /platform/v1/llm/providers/{alias}/key` | Store / clear a hosted provider's key (core → OpenBao; never logged or returned). |
| `GET /platform/v1/llm/prefs` | Stored preferences: `global_default` (chat), `global_embed_default` (embedding), `global_context_window` (num_ctx), `kv_cache_type` (Ollama KV-cache), `global_agent_max_steps` (agent loop bound), `hidden` (model list). |
| `PUT /platform/v1/llm/prefs/default` | Set or clear the global default chat model (`{model: str|null}`). |
| `PUT /platform/v1/llm/prefs/embed-default` | Set or clear the global default embedding model (`{model: str|null}`). Modules with no per-module override use this; per-module selections win (#214). |
| `PUT /platform/v1/llm/prefs/context-window` | Set or clear the **global** Ollama context window (`{value: int|null}`); the default for models without their own setting. |
| `PUT /platform/v1/llm/prefs/kv-cache-type` | Set or clear the operator's preferred Ollama **KV-cache type** (`{value: "q8_0"\|"q4_0"\|null}`, `null` = the f16 default). Server-wide; persisted, then **applied**: the core writes Ollama's start-up env file (enabling flash attention for the quantized types) and restarts the container (#307, amends ADR-0046). Returns `{value, applied}`; `applied` is `false` when Docker isn't wired, and the UI then shows the manual-restart path. |
| `PUT /platform/v1/llm/prefs/agent-max-steps` | Set or clear the agent loop bound — tool-calling rounds per turn (`{value: int|null}`, clamped 1-12; `null` = the `AGENT_MAX_STEPS` env default). Resolved per turn, no restart (#297). |
| `PUT /platform/v1/llm/prefs/hidden` | Toggle a model's hidden state (`{name, hidden}`). |
| `GET /platform/v1/llm/model-settings?model=…` · `PUT /platform/v1/llm/model-settings` | Per-model tuning (context window, keep-alive, device) for one model, chat **or** embedding. `GET` returns `{context_window, keep_alive, device}` (each `null` = inherit; `device` is `"gpu"`/`"cpu"`/`null`=auto); `PUT` body `{model, context_window, keep_alive, device}` (an all-`null` body clears the override). Persisted in Postgres (`model_settings`). See **Per-model settings** below. |
| `POST /platform/v1/llm/model-settings/suggest-context` | Compute **and persist** a recommended per-model context window for a freshly pulled model (#386), so it opens sized to itself instead of the global default. Body `{model}`. Reuses the `system/info` heuristic (VRAM-or-RAM + the named model's on-disk size + KV-cache type, capped at its trained length) but for *that* model rather than the active one. **Non-destructive** — an existing per-model context override is left untouched. Returns `{model, context_window, applied}` (`applied` is `false` when one was already set, or none could be computed — e.g. a hosted model with no local size). The web calls it when **any** pull finishes (catalog, variant, or manual tag). |
| `GET /platform/v1/system/info` | Host spec + the context-window suggestion behind the Models page. Returns `{gpu, cpu, ram_total_mb, model:{name, size_mb, context_length, quantization}, suggested_context:{min, suggested, max}, kv_cache_type}`. The suggestion estimates how big a context the box can hold from VRAM (or RAM, no GPU), the active model's on-disk size, and the **KV-cache type** (a quantized cache `q8_0`/`q4_0` costs fewer bytes/token, so the same memory buys more context). Its ceiling is the model's **trained** `context_length` when known — no longer a flat 32k — so a long-context model on a roomy GPU is no longer clipped; 32768 remains only the fallback when the trained length is unknown. Best-effort: every probe degrades to `null`. |

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

A **quant-variant lookup** (`llm/variants.py`, #330) complements the catalog: the catalog
index lists a model's parameter *sizes* but not its *quantizations*, so to pull a different
quant the operator used to have to type the exact tag. `VariantLookup` fetches the model's
public **tags page** on demand (`<LLM_CATALOG_URL>/<family>/tags`, the same host the catalog
parses) and pulls the `/library/<family>:<tag>` links for the requested size into a small
`{tag, quant}` list the Models page renders as a pick-list. (The OCI registry's `tags/list`
JSON endpoint is *not* used — `registry.ollama.ai` returns 404 for it; only the tags page
enumerates a model's quants.) It is deliberately best-effort (any failure → empty list, UI
falls back to the manual box; a model not in the public library logs at debug, not warning)
and, like the catalog, global rather than tenant-scoped.

#### Re-embedding (#332, ADR-0054)

Changing the embedding model doesn't re-embed existing data on its own — vectors built with the
old model don't match queries embedded with the new one. `POST /platform/v1/modules/reembed`
(the Models page's "Re-embed everything") **fans out** to every healthy, enabled module whose
manifest declares `reindexable` and calls its `POST /reindex`, which **drops the module's
Qdrant collection and rebuilds it** with the current embedding model in the background. The
fan-out is best-effort and returns a per-module `started`/`error` status; progress shows on
each module's `/status`. Only embedding-backed modules opt in (knowledge — covering its vault
**and** the shared module-docs collection — and notes); storage holds no embeddings. Single-
tenant in v1: each module re-embeds its own tenant's corpus, which matches the core's.

#### Per-model settings (ADR-0044)

The global context-window pref is one knob for every model; a per-`(tenant, model)`
`ModelSettingsStore` (`llm/model_settings.py`) lets the operator tune a single model — chat
or embedding — without touching the others. Three live runtime knobs are stored, all
nullable (`null` = inherit): `context_window` (Ollama `num_ctx`), `keep_alive` (how long the
runtime keeps the model loaded), and `device` (where it runs — ADR-0046).

The gateway resolves them **per call, for the model actually being used** (`_call_config`
for chat, `embed` for embeddings):

- **`num_ctx`** — the model's own `context_window` → the global `context_window` pref →
  the `LLM_NUM_CTX` env. Local models only (hosted providers never receive it).
- **`keep_alive`** — the model's own `keep_alive` → the `LLM_KEEP_ALIVE` env default.
- **`num_gpu`** — from `device`: `"cpu"` → `0` (all CPU), `"gpu"` → `999` (all layers,
  clamped by the runtime), `null`/auto → omitted (the runtime decides). Local models only.

Lookup is loose: settings keyed by the runtime's tagged name (`llama3.2:latest`) still match
a request for the bare default (`llama3.2`), and vice versa, by exact name → bare name →
family. Quantization is **not** a runtime knob — it is baked in when a model is pulled, so
the sheet shows it read-only (from `/api/show`) and offers a "pull a different variant"
shortcut instead. Embedding settings are opt-in: with nothing set, the embed call is
unchanged.

### Context compaction (fitting the prompt to the window)

A local runtime silently drops tokens past `num_ctx`, evicting the **oldest** — which is the
agent's instructions and recalled context, exactly what must survive. So before every local
call the gateway trims the prompt to fit (`llm/compaction.py`, applied in `_fit_to_context`
across the blocking + streaming paths): it keeps the leading **system** messages whole, keeps
the **most-recent** turns that fit within `num_ctx` minus a reply reserve (a bounded quarter)
and the tool-schema footprint, drops older history first, never orphans a `tool` result from
its `assistant` call, and always keeps at least the final message. When anything is dropped a
short `system` note marks the cut so the model knows earlier turns existed. Token counts are a
deliberately conservative character-based **estimate** (no tokenizer dependency, arbitrary
local models). Hosted providers — large contexts, server-side overflow handling — are left
untouched, as are calls with no known window. The common case (a short chat) is a no-op.

### Streamed tool calls

The streaming gateway (`stream_chat`) reassembles tool calls from the provider's chunks
before the agent loop runs them. Two provider shapes have to coexist: OpenAI streams one
call as partial fragments that share an `index` (the name first, then the JSON arguments in
pieces — these coalesce into one call), while Ollama streams each *complete* call with a name
but **no** `index`. Keying purely on the index collapsed every un-indexed Ollama call into one
slot and concatenated their argument strings into invalid JSON (`{…}{…}`); the corrupted
string then crashed the **next** turn when LiteLLM replayed the assistant message and ran
`json.loads` over it (`JSONDecodeError: Extra data`). So an un-indexed fragment that names a
tool now starts a fresh slot. As a backstop, every assembled call's `arguments` is normalized
to exactly one valid JSON string before it is stored or replayed — a dict is serialized, a
leading JSON value is salvaged from any trailing junk, and anything unparseable degrades to
`{}` — so a malformed stream can never poison a later turn.

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
| `POST /platform/v1/modules/reembed` | Re-embed everything (#332, ADR-0054) — the action behind the Models page's "Re-embed everything" after the embedding model changes. Fans out `POST {base}/reindex` to every healthy, enabled module whose manifest declares `reindexable` (knowledge, notes); returns `{modules: [{module, status}]}` (`started`/`error` per module). Best-effort — one module's failure never aborts the rest. |
| `GET` · `PUT /platform/v1/modules/{name}/config` | The module's config values (stored tenant-scoped in OpenBao at `modules/<name>/config`). |
| `POST /platform/v1/modules/{name}/enabled` | Enable/disable a module (#126): `{enabled: bool}`. Hides its tools, pages, and actions from the agent and shell while the container keeps running. Persisted in Postgres (`module_prefs`). |
| `DELETE /platform/v1/modules/{name}` | **Privileged** confirmed removal (#127, #382, ADR-0028): tombstone the module — which hides it everywhere and stops routing its tools at once — and tear its container down. **Decoupled from the live Docker socket** (#382): soft-removes with **200** even when the core has no Docker access, deferring the container teardown to the next startup reconcile; the response carries `container_teardown_deferred` (true when no socket was available). With a socket present it also stops + removes the container now, scoped to the core's own Compose project and refusing core-app / web / data-plane. **403** protected (enforced regardless of the socket) · **404** unknown. |
| `GET` · `PUT /platform/v1/modules/{name}/models` | Per-module model-slot selections (#128, ADR-0029): `{slot_key: model_id}`. `PUT` validates each key against the manifest's `required_models` (**400** otherwise). Persisted in Postgres (`module_prefs`). |
| `GET /platform/v1/modules/{name}/models/{slot}` | Resolve one slot to its chosen model (`null` = core default) — backs `PlatformClient.get_module_model` (#128). |
| `GET /platform/v1/modules/{name}/collections` | The module's connected accounts + collections (ADR-0030), proxied from its `GET /accounts` and **merged** with the operator's stored selection (each collection annotated `enabled`/`active`). **404** if the module declares no `collections`. |
| `PUT /platform/v1/modules/{name}/collections` | Persist the selection: `{enabled: [CollectionRef], active: CollectionRef \| null}`. Store-through (refs are not live-validated); `active` must be in `enabled` (**400** otherwise). Persisted in Postgres (`module_prefs`). |
| `GET /platform/v1/modules/{name}/collections/prefs` | The raw stored `{enabled, active}` (Postgres only, no module round-trip) — backs `PlatformClient.get_collections` so a module resolves its own routing (ADR-0030). |
| `POST /platform/v1/modules/{name}/tools/{tool}/enabled` | Enable or disable one tool (#213): `{enabled: bool}`. Hides the named tool from the agent while the module keeps running and other tools remain unaffected. **404** unknown module or undeclared tool. Persisted in Postgres (`module_prefs`). |
| `GET` · `PUT /platform/v1/modules/{name}/suggestions-enabled` | The per-module **review on/off** toggle (#KB-refactor): `{enabled: bool}`. When **on** (the default — a missing/NULL pref reads as `true`) the module stages agent changes for approval on its `review` page; when **off** the module applies them directly. The module reads this through `PlatformClient.get_suggestions_enabled()`; the shell's review-page header writes it. `PUT` **404**s an unknown module. Persisted in Postgres (`module_prefs`). |
| `POST /platform/v1/modules/{name}/tools/{tool}` | Invoke a manifest-declared UI action (runs the module's MCP tool through the host). **403** if the module is disabled. |
| `GET /platform/v1/modules/{name}/status` | Proxy the module's `ui.status_url` endpoint (returns the module's live status JSON as-is). 404 if the module is unreachable or has no `status_url`. |
| `GET /platform/v1/modules/{name}/read?path=…` | Proxy a module's `GET /read` text-file endpoint for the Files split-screen reader (#KB-refactor): `{path, name, content}`. Upstream 4xx pass through (415 binary, 413 too large, 404 missing); an unreachable module is a controlled **502**. |
| `POST /platform/v1/modules/{name}/pages/{page_id}/project?project=…` | Create a new knowledge base (project / top-level scope) in an editor page's store (#KB-refactor). 409 if it exists, 400 for an invalid name; the module enforces name-safety. |
| `POST /platform/v1/modules/{name}/pages/{page_id}/suggestions/{id}/approve` | Approve a staged suggestion — the module applies + indexes it (#220, ADR-0033). Optional `{content}` body is the operator's **per-hunk-merged** result for an edit, forwarded so only the approved changes are written (#KB-refactor). Operator-only. |
| `POST /platform/v1/modules/{name}/pages/{page_id}/suggestions/{id}/reject` | Reject a staged suggestion — the module discards it, nothing written (#220). Operator-only. |
| `GET /platform/v1/suggestions` | **Cross-module pending-suggestions feed** (#KB-refactor): every enabled module with a `review` page — the knowledge base **and** private **notes** — each item tagged with `module` + `page_id`. `operation` ∈ `create`/`update`/`append`/`delete`/`move`/`mkdir`/`mkproject` (`append` is notes-only — the agent supplies just the text to add). Best-effort aggregation — a down / disabled / erroring module is skipped, not fatal. Backs the chat composer's suggestion bubble and the Suggestions page. (Lives at `/platform/v1/suggestions`, not under `/modules`.) |

> **Privileged surface (ADR-0028, #307, #382).** Tearing down a removed module's container — and
> applying the Ollama KV-cache type — needs the Docker socket, mounted read-write on `core-app`
> **only**. The core touches it through a single `DockerController`: it stops/removes **only a
> configured module's own container**, and separately **restarts only an allowlisted infra
> container** (`ollama`, which is never removable). Both are scoped to this Compose project and
> never touch core-app / web / a data-plane service. Module **removal itself no longer needs the
> socket** (#382): it tombstones the module (hidden + unrouted at once) and **defers** the
> container teardown to the next startup reconcile when the socket is absent — so dropping the
> mount leaves removal working (the container lingers until the next restart), while a KV-cache
> change then saves without applying.

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
| `ASK_USER_TTL_HOURS` | `24` | How long a turn paused by `ask_user` waits for an answer before its suspended run is reaped (ADR-0053). |
| `LIVE_RUN_GRACE_SECONDS` | `300` | How long a *finished* in-flight run stays re-attachable in memory before it is reaped (ADR-0055). Pure cache — the answer is already durable, so this only bounds how long a late re-attach can tail the buffer. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Conversation persistence. |
| `QDRANT_URL` | `http://qdrant:6333` | Semantic-recall vectors. |
| `MEMORY_EMBED_MODEL` | `nomic-embed-text` | Local embedding model for recall. |
| `MEMORY_EXTRACTION_MODE` | `nightly` | When fact extraction runs: `nightly` (deferred to a queue drained off-hours, ADR-0051) or `immediate` (a background task after each turn, ADR-0045). |
| `MEMORY_EXTRACTION_HOUR` | `3` | Local hour (0-23) of the nightly drain, in the operator's timezone. |
| `MEMORY_EXTRACTION_MODEL` | — | Optional small dedicated model for the extraction call (e.g. `llama3.2:3b`); blank = the default chat model. |
| `MEMORY_EXTRACTION_BATCH_LIMIT` | `200` | Max exchanges distilled per nightly drain. |
| `MEMORY_RECALL_TIMEOUT_S` | `4.0` | Time-box (seconds) for the inline recall embed before a turn proceeds without it (ADR-0051). 4s (was 2s) fits a single-GPU embed-model swap. |
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
  `global_embed_default` (embedding model, #214), `context_window` (global `num_ctx`),
  `kv_cache_type` (Ollama KV-cache, ADR-0046), `agent_max_steps` (agent loop bound, #297),
  `hidden_models` (JSON list). A missing row means all defaults are `null` (fall back to env
  settings).
- **Postgres `model_settings`** — per-`(tenant, model)` tuning (ADR-0044/0045):
  `context_window`, `keep_alive`, and `device` (`"gpu"`/`"cpu"`/`null`), all nullable
  (`null` = inherit). Drives the per-model resolution chain in the gateway (see **Per-model
  settings**). A missing row means the model inherits the global pref / env defaults.
- **Postgres `module_prefs`** — per-`(tenant, module)` operator preferences: `enabled`
  holds the enable/disable flag (#126), `removed` tombstones a module after its container is
  deleted (#127), `models` holds per-slot model choices (#128), `disabled_tools` holds a JSON
  list of tool names the operator has toggled off (#213), `collections` holds the
  account/collection selection (`{enabled, active}` JSON, ADR-0030), and `suggestions_enabled`
  holds the per-module review on/off toggle (#KB-refactor; NULL ⇒ on). A module with no row
  defaults to enabled, not-removed, core-default models, all tools on, review on, and the local
  default collection. Post-release columns are added in place at startup (no migration framework).
- **Postgres `timezone_prefs`** — per-tenant IANA timezone for the `now` tool (ADR-0039):
  `tenant`, `timezone`. A missing row (or null) falls back to `DEFAULT_TIMEZONE`.
- **Postgres `agent_suspended_runs`** — a turn paused by `ask_user` (ADR-0053): `id` (run_id),
  `tenant`, `session_id`, `model`, `pending_call_id`, `question`, `conversation` (JSON),
  `created_at`. Written on suspend, **consumed** on resume, reaped after `ASK_USER_TTL_HOURS`.
- **In-memory live runs** (`LiveRunRegistry`, ADR-0055) — *not* persisted: each in-flight turn's
  detached task + its seq-tagged event buffer, keyed by `run_id` and indexed by `(tenant,
  session_id)`. Disposable cache for re-attach; the authoritative answer lands in `agent_messages`.
  Lost on restart (recover an interrupted turn via regenerate); reaped after `LIVE_RUN_GRACE_SECONDS`.
- **Qdrant `<tenant>__facts`** — durable **facts about the user** for cross-chat recall
  (cosine), one collection per tenant (ADR-0045). Each point is a short standalone fact
  under an opaque UUID id, payload `{text, source, created_at}` (`source` = `tool` | `auto`).
  Facts are written by the `remember` tool and by background extraction, deduped on write
  (cosine ≥ 0.92); recall searches this collection, and the **Settings → Memory** box lists /
  searches / forgets it. Raw conversation turns are **not** indexed — the verbatim transcript
  lives only in `agent_messages`. (The pre-ADR-0045 recall collection `<tenant>__memory` is no
  longer written; any existing vectors are simply unused.)
- **Postgres `memory_extraction_queue`** — finished exchanges awaiting background fact
  extraction (ADR-0051): `id`, `tenant`, `user_text`, `assistant_text`, `created_at`. In the
  default **nightly** mode the agent enqueues each exchange here instead of distilling it inline;
  the `ExtractionRunner` drains it once a day (at `MEMORY_EXTRACTION_HOUR` in the operator's
  timezone), serially, so extraction never competes with a live turn for the GPU. Drained rows
  are deleted; because the queue is durable, a restart never loses a pending exchange.

Memory is **best-effort**: if Postgres, Qdrant, or the embedder is down, a turn still
answers — just without memory — and never blocks core startup. Recall (the one memory step left
on the response path) is **time-boxed** (`MEMORY_RECALL_TIMEOUT_S`, 4s — long enough for a
single-GPU embed-model swap) so a cold or busy embedder can't stall the first token; a timed-out
recall logs `recall skipped: embed timed out` and a backend failure `recall skipped: backend
error`, so the two are told apart at a glance. Fact extraction never runs on the response path: by default it is
**deferred** to a nightly drain (ADR-0051) so it can't compete with a live turn for the GPU —
set `MEMORY_EXTRACTION_MODE=immediate` to distil as a background task right after each turn
instead (the original ADR-0045 behaviour). A dedicated small `MEMORY_EXTRACTION_MODEL` keeps the
distillation cheap and off the chat model.

## Dependencies

Ollama (models) · Postgres (memory) · Qdrant (recall) · OpenBao (provider + module
secrets) · NATS (usage events) · the modules in `MODULE_URLS` (tools, over MCP).

## Run & extend

```bash
docker compose up -d core-app      # comes up with the full stack
```

Source is one package, `epicurus_core_app`, split by responsibility: `agent/`
(loop + MCP host + routes), `llm/` (gateway, providers, power, models), `memory/`
(store + facts + extraction + facade), `modules.py` (registry), `platform_api.py` (inference
endpoints), `app.py` (wiring). The agent targets only the gateway's interface and
modules only through MCP — never a provider SDK.
