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
| `POST /platform/v1/agent/chat/stream` | The same turn as **SSE**: an optional leading `readiness` (warming progress, ADR-0027) · `delta` (answer tokens) · `thinking` (chain-of-thought tokens, ADR-0041) · `tool` (a tool ran) · `awaiting_input` (the turn paused — for `ask_user` it carries `{run_id, question}`, ADR-0053; for a **draft-first send** it carries `{run_id, awaiting_kind: "draft_review", draft}`, ADR-0085/#563 — an additive shape a stale client ignores) · `done` (final turn) · `error`. Each data frame carries an `id:` (a live-run seq) for re-attach. The turn runs **decoupled from this connection** (ADR-0055): a disconnect doesn't abort it — the answer still persists and the client re-attaches. A turn already running for the session yields **409** (+ `X-Run-Id`). The web shell speaks this. |
| `GET /platform/v1/agent/sessions` | List conversations (title + last-active + count). |
| `GET /platform/v1/agent/sessions/{id}` | A session's full transcript. |
| `GET /platform/v1/agent/sessions/{id}/active-run` | The session's in-flight run to re-attach to — `{run_id, last_seq}` or `null` if none is live (ADR-0055). How a client rediscovers a turn after a reload/reconnect. |
| `DELETE /platform/v1/agent/sessions/{id}/active-run` | Cancel the session's in-flight turn — the explicit **Stop** (a decoupled turn outlives the connection, so Stop must say so). Returns `{cancelled}` (ADR-0055). |
| `GET /platform/v1/agent/active-runs` | Session ids with an in-flight turn right now — `{session_ids}`. Drives the conversations-list running indicator (#396) in one request rather than polling each row; tenant-scoped, best-effort/point-in-time (the live-run buffer is a disposable cache). |
| `DELETE /platform/v1/agent/sessions/{id}` | Forget a conversation — its history rows. Facts the user is remembered by are kept (ADR-0045). |
| `POST /platform/v1/agent/sessions/{id}/regenerate` | Re-answer the session's last user turn, dropping the previous answer. Body `{model?}`. Truncates everything after the last user message, then streams a fresh turn — same SSE protocol as `/chat/stream`; an `error` event if there's no user turn (#302). |
| `POST /platform/v1/agent/sessions/{id}/edit` | Replace the last user message with `{content}` (and `{model?}`) and re-answer it in place — edits the message, truncates the tail, then streams. An `error` event on empty content or no user turn (#302). |
| `POST /platform/v1/agent/runs/{run_id}/resume` | Resume a turn paused by `ask_user`, supplying `{answer}` (ADR-0053). Consumes the suspended run, appends the answer as the pending tool call's result, and continues the same turn — same SSE protocol as `/chat/stream`. An `error` event if the run is unknown / expired / already answered. |
| `POST /platform/v1/agent/runs/{run_id}/draft` | **Confirm/Decline a draft-first send** (ADR-0085, #563). Body `{decision: "send" \| "decline", reason?}`. Consumes the pending draft; on `send` the core transmits it via the owning module's `POST /send` and appends the outcome (`Sent.` + id, or a relayed error hint) as the compose call's tool result, on `decline` it appends a "not sent" result (carrying any `reason`) — then continues the same turn (same SSE protocol as `/chat/stream`). An `error` event if the draft is unknown / expired / already resolved. Confirm/Decline is connection-gated client-side (#530); the `run_id` is the DB pause token, distinct from a live-run id. |
| `GET /platform/v1/agent/runs/{run_id}/stream?after_seq=N` | **Re-attach** to an in-flight turn (ADR-0055), replaying buffered events after `N` (or `Last-Event-ID`) then tailing live — same SSE protocol as `/chat/stream`, no readiness prelude. A `gone` event if the run is unknown / finished-and-reaped (the client then falls back to the durable transcript). Note: this `run_id` is a **live-run** id (in-memory, for re-attach), distinct from the suspended-run id used by `/resume`. |
| `GET /platform/v1/agent/memory?q=&limit=` | The cross-chat memory corpus — the durable **facts** the model remembers about the user (ADR-0045). No `q`: the facts newest-first; with `q`: what recall surfaces for that query (the same ranking a turn gets). Returns `{items, total}` — each `MemoryItem` is `{id, text, source, created_at?, score?}` where `source` is `tool` (the `remember` tool) or `auto` (background extraction); `score` is set only for a search. `limit` is bounded 1–500 (default 200). Backs the **Settings → Memory** box. |
| `DELETE /platform/v1/agent/memory/{id}` | Forget one remembered fact so it stops being recalled. Drops its vector; the conversation that surfaced it is untouched. Returns `{forgotten}`. |
| `GET /platform/v1/agent/memory/profile` | The **standing profile** the agent injects each turn (#527, ADR-0094). Returns `{profile, source, pinned, versions}` — `profile` is `null` before first synthesis (the agent then behaves exactly as before); `source` is `auto` (nightly synthesis) or `edited`; `pinned` flags an operator edit that survives re-synthesis; `versions` is the recent history. Backs the **Settings → Memory** standing-profile panel. Declared **before** `/memory/{id}` so a DELETE isn't captured as "forget the fact `profile`". |
| `PUT /platform/v1/agent/memory/profile` | Save an operator edit (`{content}`) — stored as an `edited`, **pinned** version the nightly synthesizer won't clobber. A blank body **clears** the profile (resume auto-synthesis), same as DELETE. |
| `DELETE /platform/v1/agent/memory/profile` | Clear the profile (all versions); the next nightly synthesis regenerates a fresh `auto` one. Returns `{cleared}`. |
| `POST /platform/v1/agent/attachments` | Upload a file to attach to a turn → its core-side handle (`att_id`). Capped at `ATTACHMENT_MAX_BYTES` (10 MiB; **413** over) with a content-type allowlist (`ATTACHMENT_ALLOWED_TYPES`; **415** if disallowed); best-effort mirrored to the storage sink (ADR-0025). An `image/*` upload rides the turn as real multimodal content when the selected model supports vision (#633) — see below. |
| `GET /platform/v1/agent/instructions` · `PUT /platform/v1/agent/instructions` | The agent's editable **base system prompt** (#497, ADR-0083). `GET` → `{instructions, is_default}` (the effective prompt — stored value else the shipped default — and whether it's the default). `PUT {instructions}` sets it; a `null`/blank body **resets** to the default. Optional `tenant_id`. Resolved per turn (no restart) and injected as the **first** message of every turn (chat + headless), ahead of recalled memory and attached context, so the compaction prefix rule protects it. Persisted in `agent_instructions`; edited in **Settings → Assistant instructions**. These routes read and write the **base prompt alone** — the enabled playbooks composed onto it for the turn (ADR-0093 §4, see *Governed playbooks* below) are not part of this editable document. Each `PUT` snapshots the prompt it replaced, so an edit is undoable (ADR-0046). |

Tools are offered to the model **only when it can use them**: the loop checks the resolved
model's capabilities (`gateway.supports_tools` → `/api/show`; hosted providers are assumed
capable) and, for a tool-less local model, calls without tools so the turn falls back to a
plain text answer instead of the runtime erroring. The web shell surfaces the same fact as a
"can't use tools" hint in the composer.

**Image attachments are gated on vision support the same way — but stricter (#633).** An
uploaded `image/*` file never goes through the text-attachment expander (decoding it as UTF-8
would just produce replacement-character noise); it resolves separately to an `ImagePart` and,
just before the provider call, is spliced into the user message as OpenAI-style multimodal
content parts (`[{type: "text", ...}, {type: "image_url", ...}]`) — never into what gets
persisted, so a stored turn never balloons with base64 image data. LiteLLM's own provider
adapters translate that shape per backend (a local `ollama_chat` call becomes Ollama's `images`
field), so no per-provider branching is needed here. The gate itself
(`gateway.supports_vision`) differs from `supports_tools` in two ways because the failure mode
is worse — a mis-sent image either gets silently ignored or draws a provider 400, the exact
thing this exists to prevent: hosted providers are **not** assumed capable (LiteLLM's own
model-cost map is asked instead of guessing), and a local model with no reported capabilities
defaults to **not** vision-capable rather than "don't restrict". When the check fails, the turn
never reaches the provider at all — it ends immediately with a canned explanation
(`stopped="unsupported_media"`), the same shape as any other turn (persisted, streamed as a
normal answer), just skipping the extraction hand-off (a canned rejection is nothing to learn
facts from).

**Tool results that carry entity refs also teach the model the ids** (ADR-0079). When a module
tool returns an envelope (`tool_envelope(text, [EntityRef…])`), the loop lifts the refs onto the
turn for UI chips — and appends a compact `title → id` listing to the tool result the **model**
sees, so a "list, then act on one" flow (list events → `calendar_update_event`) has a real id to
pass back. The block is model-only context: never rendered in chat, never part of the display
text. The module-author side of this contract is in
[the modules reference](../reference/modules.md).

**The id block is capped at `LIST_CAP` (50) refs** (ADR-0084, #468): past that, it truncates
with a "showing 50 of N — narrow the query/range or ask for more" note (logged with the
tenant id) instead of echoing an unbounded list into the model's context — a large result
(a wide search, RRULE-expanded calendar events over a long window) previously roughly
doubled its context cost once every ref's id was echoed too. The full ref list still
reaches the UI's chips (`AgentTurn.entity_refs`) unchanged; only the model-facing text is
bounded. `epicurus_core.capped_listing` lets a module cap its own hand-built list text the
same way — `calendar_list_events` is the first adopter.

A turn **never ends silently empty.** A reasoning model sometimes emits its `<think>` block and
then stops — no answer text, no tool call — which would persist as an empty turn and render as a
silent "stop". The loop nudges such a step once to commit to an answer, then (if it still says
nothing, even on the forced final round) substitutes a clear fallback message and logs `turn
produced no answer; using fallback` with whether the model reasoned and whether it was nudged.

**Loop hygiene — outcome-aware early stops (ADR-0091).** Two tool-call shapes used to burn the
whole `max_steps` budget and end in the same silent stop: the model re-issuing the **exact same**
call over and over, and a **streak of tool errors** (retrying a broken call to exhaustion). A small
`_LoopGuard` wraps the loop (ADR-0001 stays thin — this is outcome-aware *stopping*, not planning),
applied identically to `run` and `run_stream`:

- **Repeated identical call** — each step's calls are canonicalized to an order-free
  `(name, sorted-args)` signature; matching the immediately previous step, the **first** repeat gets
  a one-shot nudge (like the empty-answer nudge) and is **not re-executed** (a repeated *write* would
  double-apply — the earlier result already stands), and a **further** repeat ends the turn with
  `stopped="repeat_call"`. Comparing *arguments* leaves a legitimate distinct-args repeat (paging,
  per-item work) untouched.
- **Error streak** — three consecutive tool errors end the turn with `stopped="tool_errors"`; **any**
  success resets the streak, so a turn that errors once and recovers is unaffected.

Either early stop then takes the **same single tool-less final round** `max_steps` already uses, so
the turn ends with a real answer — "here's what I found / what failed" — never a silent stall. So
`AgentTurn.stopped` is now one of `completed` · `max_steps` · `repeat_call` · `tool_errors` ·
`unsupported_media` (an image attachment blocked before any provider call, #633; plus `error` on a
mid-stream failure, streaming only); the streamed `done` event carries it for the web to key
stop-reason copy off. The repeated / errored tool steps stay in the activity timeline (errors
render red), so the process that led to the cut is visible.

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

### Governed playbooks (ADR-0093)

The agent's behaviour used to improve only when the operator hand-edited the base prompt.
**Playbooks** capture what the system learns in use — recurring corrections, discovered
procedures ("for a morning briefing, check calendar before mail") — as durable guidance, without
ever letting the agent rewrite itself. The rule is absolute: the nightly reflection pass
**proposes**, the operator **approves**, and only an approval writes. Nothing self-applies.

**What a playbook is.** A named, independently enable-able block of guidance stored beside the
base prompt (`agent_playbooks`), rather than more text crammed into one monolithic instruction
string. Add or silence one without touching the rest.

**How guidance reaches the model.** `Agent._assemble` is unchanged: it calls
`AgentInstructionsStore.get_instructions(tenant)` and leads the turn with whatever string comes
back. What changed is what that method *composes* — the base prompt, then every **enabled**
playbook under a `## Playbook: <name>` heading (so the model can attribute guidance to its
source), returned as one opaque string. Composition therefore happens *below* the accessor, which
is why the assembly path needed no change at all. Playbooks are ordered oldest-first then by
name — a total, stable order, so the prompt never reshuffles between reads. Enrichment is
best-effort: if the playbook read fails the turn proceeds on the base prompt alone rather than
breaking. Token budget follows ADR-0083's precedent — an informal, UI-side soft-size warning over
the *combined* length, not a hard server-side cap.

**The approval surface.** A proposal is an ordinary `ReviewSuggestion`
([`epicurus_core.review`](../reference/platform-api.md), ADR-0090) — `operation: "update"`
against the base instructions or an existing playbook, `"create"` for a new one — so the existing
`ReviewView` / `SuggestionReviewModal` render it with the same diff, editable draft, and audit
trail every module's queue gets. Approve applies the (possibly hand-edited) content through the
stores below; reject discards. Both record a durable decision row.

**The reserved `core` pseudo-module.** Every other `review`-page implementer is an external
module the registry reaches over HTTP; the core hosts no page of its own. Rather than bend the
core into a module that calls itself over the network (rejected in the ADR as needless
indirection), `ModuleRegistry` accepts one **reserved entry named `core`** that it answers
**in-process** — see *Module registry* below. It rides `GET /platform/v1/modules` so the shell
discovers its page like any module's, with no new endpoint and no new frontend contract.

**Storage and undo.** An approved edit to the *base* prompt writes through the **existing**
`AgentInstructionsStore` — the same path the operator's own Settings edit uses, so an approved
edit is indistinguishable from a hand-typed one. Both halves version ADR-0046-style
(snapshot-on-save, capped at the same `MAX_VERSIONS = 50`, oldest pruned). One deliberate
departure from the editor's version store: it snapshots the content *being saved*; these
snapshot the content being **replaced**. The editor accumulates many operator saves, so the prior
body is always somewhere in its history; here the very first write may be an approved
agent-authored edit against a body never saved through this path, and recording only the new
content would leave the original unrecoverable — exactly the undo the ADR says an agent-proposed
edit needs. A save that changes nothing records no version.

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
| `GET /platform/v1/timezone` | The operator's effective IANA timezone (stored value, else `DEFAULT_TIMEZONE`); tenant-scoped via an optional `tenant_id` query param, falling back to the default tenant. |
| `PUT /platform/v1/timezone` | Set the timezone (`{timezone}`; validated as a real IANA zone, **400** otherwise); same `tenant_id` scoping. Edited in the web **Settings → Timezone** card. |

- **`remember(fact)`** — save a durable fact about the user to long-term memory (ADR-0045).
  The agent's explicit, *hot-path* way to remember: it calls this when the user says
  "remember…" or it learns a stable detail/preference. The fact is written to the user-fact
  store (`source=tool`) for the **calling tenant** — built-in handlers receive the tenant
  precisely so `remember` can scope its write. A near-duplicate of an existing fact is a
  no-op. The *implicit* path is background extraction — deferred to a nightly drain by default
  (ADR-0051; see **Data model**); together they are the corpus that recall pulls into later chats.
- **`memory_search(query, scope?, limit?)`** — deliberate recall over long-term memory
  (ADR-0089). The complement to the ambient recall `_assemble` injects each turn: the agent
  calls this when the user refers to something discussed or decided *before* that isn't in the
  current conversation ("what did we settle on for the backup strategy?"). `scope` ∈
  `facts | sessions | both` (default `both`) chooses the **fact store** (Qdrant — the same
  ranking a turn's recall gets), past **conversations** (a portable case-insensitive content
  match over `agent_messages`, joined to each conversation's title + date), or both. `limit` is
  clamped 1–10 (default 5) per source. Runs for the **calling tenant** only (recall crosses
  sessions, so scoping is a privacy boundary, constraint #1). Best-effort like all memory: the
  facts half embeds through the gateway (constraint #8), so a cold embedder degrades to just the
  sessions text search (no embed) rather than failing the call; results are capped and compact —
  never a raw session dump. It runs inline like `now`/`remember` and shows as a normal
  `memory_search` step in the activity timeline.
- **`ask_user(question)`** — pause the turn to ask the operator a clarifying question
  (ADR-0053). Unlike other built-ins it is **not executed inline**: the agent loop intercepts
  the call, persists the in-progress run (`agent_suspended_runs`), emits an `awaiting_input`
  SSE event, and ends the stream. The web shows the question + an input; the answer is POSTed
  to `…/agent/runs/{run_id}/resume`, which rehydrates the run and continues the same turn with
  the answer as the tool result. The suspended run is consumed on resume and reaped after
  `ASK_USER_TTL_HOURS`. With no suspend store wired the loop degrades — the model is told to
  proceed with its best assumption rather than pausing.

The same pause machinery powers **draft-first outbound sends** (ADR-0085, #563) — but triggered by
a *module* tool, not a core built-in. When a compose tool (mail's `mail_send` / `mail_reply`)
returns a `DraftReview` envelope (`epicurus_core.draft_review`), the loop recognizes it the way it
lifts `entity_refs` from a `ToolEnvelope` and **suspends the turn** instead of feeding it back to
the model: it persists the run + composed draft to `agent_pending_drafts` (a sibling of
`agent_suspended_runs`, reaped after `DRAFT_REVIEW_TTL_HOURS`) and emits `awaiting_input` with
`awaiting_kind: "draft_review"` + the draft. The operator's **Confirm** (`POST …/runs/{id}/draft`,
`{decision: "send"}`) makes the core transmit the exact draft via the module's `POST /send`
(`ModuleRegistry.send_draft`) and resume with the outcome; **Decline** resumes with a "not sent"
result (+ any reason). The MCP surface exposes **no** transmitting tool, so the model can compose
but can never send — the guarantee is structural. Any future outbound channel (Phase-4 chat
bridges) opts in by returning the same envelope and serving its own `/send`. Only the interactive
streaming path can present a draft; the **non-streaming** loop (`POST /chat`, the messaging bridge)
has no review pane, so it degrades — the model is told the draft can't be sent from that channel
rather than being fed the raw envelope (nothing is transmitted regardless).

### LLM gateway (ADR-0010)

The gateway's HTTP surface is **model/provider management** (consumed by the web UI).
Chat completions go through `POST /platform/v1/chat` above (ADR-0021); the gateway's
own `POST /platform/v1/llm/chat` was **removed in `core-app` 0.2.0** — it duplicated
`/chat` (which is a strict superset: it also accepts `tools` + `tenant_id`).

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/llm/models[?capabilities=true]` · `DELETE /platform/v1/llm/models?name=…` | List / remove local models (the `loaded` flag marks in-memory ones). `?capabilities=true` additionally fills each model's reported `capabilities` (e.g. `tools`, `vision`) and trained `context_length` (#618) from `/api/show` — opt-in (one call per model), so the Models page can badge them and show a context-window chip while the chat picker stays light. `context_length` is `null` when the runtime doesn't report it — never a fake default. |
| `GET /platform/v1/llm/models/details?model=…` | Read-only facts about a model: `{quantization, parameter_size, context_length, family, capabilities}` (any field `null`/empty when not reported — never a fake default). Local models read the runtime's `/api/show`; **hosted** models (#633/#618) read LiteLLM's own model-cost/context map instead (no provider call) — `quantization`/`parameter_size`/`family` stay `null` there (Ollama-only concepts), `capabilities` always includes `tools` (hosted providers are assumed tool-capable) plus `vision` when LiteLLM's map says so. Backs the model-settings sheet, the Models page's context-window chip, and the chat "can't use tools" / "can't see images" hints. `model` is a query param (names carry `:`/`/`). |
| `GET /platform/v1/llm/catalog` | The browsable model catalog the core parses from upstream on a schedule (#269). Returns `{entries[], source, updated_at, stale}`; each entry's `size_gb` is the **real on-disk size** backfilled from its family's tags page (#571; `null` until the size fill or a variant lookup reaches the family, and always `null` for `cloud` rows). `stale` flags a seed / last-good list served after a failed or skipped refresh. See **Model catalog** below. |
| `GET /platform/v1/llm/catalog/variants?model=…` | The quant variants available for a model (#330), looked up on demand from the model's public library **tags page** (the catalog index lists *sizes*, not quants). Returns `{model, variants:[{tag, quant, size_gb}]}` — `size_gb` is the tag row's real on-disk size (#571; `null` when upstream shows none, e.g. a cloud alias). Best-effort — an empty list (offline, or a model not in the public library) makes the UI fall back to a manual tag box. A successful lookup also piggybacks its sizes onto the catalog snapshot. `model` is a query param. See **Model catalog** below. |
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
| `GET /platform/v1/llm/saved-models` · `POST` · `DELETE ?model=…` | The tenant's **saved hosted-model ids** (#496). `GET` → `{models:[{model, provider, context_length, capabilities}]}` (most-recent-first) — `context_length`/`capabilities` (#618) come from the same LiteLLM model-cost lookup as `/models/details`, always included (a static lookup, not a network call, so unlike the local list this isn't gated behind an opt-in query param); `null`/empty when the model isn't in LiteLLM's map. `POST {model}` persists one, idempotent — an atomic upsert (**400** if it isn't a hosted `<provider>/<model>` id, so a local `hf.co/…` **or** a provider-only `claude/` with no model can't land). `DELETE ?model=…` forgets one (removing the id that is the current global default leaves `llm_prefs.global_default` pointing at it — still valid for inference, just unlisted). Backs the chat picker (auto-saved on use), the Models page (remove / set-as-default), and module model slots; persisted in `saved_models`. Mutations **503** without the store. |
| `GET /platform/v1/llm/model-settings?model=…` · `PUT /platform/v1/llm/model-settings` | Per-model tuning (context window, keep-alive, device) for one model, chat **or** embedding. `GET` returns `{context_window, keep_alive, device}` (each `null` = inherit; `device` is `"gpu"`/`"cpu"`/`null`=auto); `PUT` body `{model, context_window, keep_alive, device}` (an all-`null` body clears the override). Works for a **hosted** `<provider>/<model>` id too — there `context_window` is a **compaction budget** (`keep_alive`/`device` are local-only Ollama options). Persisted in Postgres (`model_settings`). See **Per-model settings** below. |
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

**Cloud-only models** (#571): some upstream families publish no downloadable weights at all —
their only tag is a `cloud` alias whose inference runs on the library vendor's cloud. The
index marks them with a `cloud` pill (a plain styled span **without** the `x-test-capability`
hook, so the parser matches it separately; verified live 2026-07-09). The parser adds `cloud`
to the tag vocabulary (alongside the `thinking` capability, new in the same pass) — but only
on a family's **size-less bare entry**: hybrid families (gemma3, gpt-oss, …) carry the pill
too, yet their size-expanded rows are ordinary local builds and stay untagged. The web badges
`cloud` rows, offers no Pull, and excludes them from fit — by design, with the reason in a
tooltip.

A **quant-variant lookup** (`llm/variants.py`, #330) complements the catalog: the catalog
index lists a model's parameter *sizes* but not its *quantizations*, so to pull a different
quant the operator used to have to type the exact tag. `VariantLookup` fetches the model's
public **tags page** on demand (`<LLM_CATALOG_URL>/<family>/tags`, the same host the catalog
parses) and pulls the `/library/<family>:<tag>` links for the requested size into a small
`{tag, quant, size_gb}` list the Models page renders as a pick-list — `size_gb` is the
**real on-disk size** shown on the tag's row (#571; `null` for cloud aliases, which publish
none), so the pick-list and its fit badges use real sizes instead of bits-per-weight
estimates. (The OCI registry's `tags/list` JSON endpoint is *not* used — `registry.ollama.ai`
returns 404 for it; only the tags page enumerates a model's quants.) Parsed tag rows are
**cached per family** (TTL = the catalog refresh interval), so repeated lookups cost one
upstream request. It is deliberately best-effort (any failure → empty list, UI falls back to
the manual box; a model not in the public library logs at debug, not warning) and, like the
catalog, global rather than tenant-scoped.

**GB size fill** (#571): the index page publishes no on-disk sizes, so a fresh catalog parse
has `size_gb = null` everywhere — only the tags pages carry sizes. Rather than an eager crawl
(the refresh stays **exactly one** upstream request), a background fill walks the families
most-popular-first, **one rate-limited tags-page lookup per `LLM_CATALOG_SIZE_FILL_SECONDS`**
(default 30 s; `0` disables), through the variant lookup's shared per-family cache. A sized
row takes its bare `<size>` tag's size (the default build); a size-less downloadable family
(embedding models) takes `latest`; `cloud` rows are skipped by design. Each successful
refresh restarts the walk, and enriched sizes are **carried across refresh swaps** so GB
labels never flap back to empty. A tags-page failure just leaves that family size-less until
the next pass — it never blocks or empties the catalog. On-demand variant lookups piggyback
their freshly cached sizes onto the catalog immediately, ahead of the walk.

#### Re-embedding (#332/#436, ADR-0054/ADR-0074)

Changing the embedding model doesn't re-embed existing data on its own — vectors built with the
old model don't match queries embedded with the new one. `POST /platform/v1/modules/reembed`
(the Models page's "Re-embed everything") **fans out** to every healthy, enabled module whose
manifest declares `reindexable` and calls its `POST /reindex`, which **drops the module's
Qdrant collection and rebuilds it** with the current embedding model in the background. The
fan-out is best-effort and returns a per-module `started`/`error` status; progress shows on
each module's `/status`. Only embedding-backed modules opt in (knowledge — covering its vault
**and** the shared module-docs collection — and notes); storage holds no embeddings. Single-
tenant in v1: each module re-embeds its own tenant's corpus, which matches the core's.

Memory facts aren't a module and don't have a `/reindex` endpoint, but they're just as
model-dependent, so they're folded into the same action a different way (#436, ADR-0074): the
**maintenance orchestrator**'s `facts-reembed` job (below) calls `UserFactStore.reembed_all`
directly (core-resident, no HTTP hop) as part of the manual "run everything" trigger. Unlike a
module's drop-and-recrawl, this **preserves each fact's id and text and only replaces the
vector** — a fact has no source document to cheaply recrawl the way a knowledge doc does. The
reconcile pages through the *entire* collection rather than scanning a single bounded batch, so
every fact is preserved regardless of how large the corpus has grown (#450, ADR-0076). The
same reconcile also runs **lazily and automatically**: `UserFactStore._ensure` compares a
collection's actual vector size against the current embedder's on first use each process
lifetime, and self-heals a mismatch on the spot — so recall/save survive a model swap even
before anyone clicks "Re-embed everything".

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

Lookup is loose **for local models**: settings keyed by the runtime's tagged name
(`llama3.2:latest`) still match a request for the bare default (`llama3.2`), and vice versa, by
exact name → bare name → family. Quantization is **not** a runtime knob — it is baked in when a
model is pulled, so the sheet shows it read-only (from `/api/show`) and offers a "pull a
different variant" shortcut instead. Embedding settings are opt-in: with nothing set, the embed
call is unchanged.

A **hosted** model reuses the same row (keyed by its full `<provider>/<model>` id) for one knob
only: `context_window`, read as a **compaction budget** rather than `num_ctx` — see
[Context compaction](#context-compaction-fitting-the-prompt-to-the-window) (#570). `keep_alive`
and `device` are Ollama runtime options and stay local-only; the Models-page settings sheet for
a saved hosted model shows the context field alone.

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
local models). The common case (a short chat) is a no-op.

The window means different things per provider class, so the two resolve it differently
(`_fit_to_context`):

- **Local** — a runtime *allocation* (`num_ctx` → KV-cache memory). The window is the
  model's own `context_window` → the global pref → the env (`_effective_num_ctx`).
- **Hosted** — a *budget* (#570). A hosted provider fixes the real window and **rejects** an
  over-window request, so compacting to the operator's per-model `context_window` both prevents
  that `context_length_exceeded` failure and caps per-turn input spend (every turn resends the
  window). Resolved by **exact model id** from the same `model_settings` row — and **only**
  that: never the global `context_window` pref (a *local* `num_ctx` knob; an 8k local value must
  not silently over-compact a 200k hosted model) and never a loose family match (so a hosted
  `custom/llama3.2` can't inherit a local `llama3.2:latest` window). With no per-model budget
  set, hosted calls are left **untouched** — today's behavior. The budget never enters the
  hosted API call; `num_ctx` stays local-only.

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

### Stream timeouts & mid-stream failures (#453)

Every `litellm.acompletion` call carries an explicit timeout (`LLM_TIMEOUT`, default **1800s**),
built once as `httpx.Timeout(read=LLM_TIMEOUT, connect=30s)` and passed at all three call sites
(`_complete`, `stream`, `stream_chat`). The **read** component is what matters for streaming:
LiteLLM threads it down to aiohttp's `sock_read`, which fires on the gap *between* stream chunks.
On a single-GPU box the pre-first-token window — a cold model load plus prompt-eval, worst on the
first long generation after tool/embed activity forces a model swap — legitimately stalls token
flow for minutes; too low a read timeout aborts a valid generation mid-stream with
`Timeout on reading data from socket`. The default is generous so a long knowledge-doc generation
completes; lower it for faster failure, or set `LLM_TIMEOUT=0` to remove the inter-chunk bound
entirely (mapped to a large finite read, never `None`: `ollama_chat` is outside LiteLLM's
`supports_httpx_timeout()` allowlist, so `CompletionTimeout.resolve()` collapses our
`httpx.Timeout` to its `.read` component and substitutes its own 600s fallback whenever that
component is `None` — verified against the pinned litellm 1.89.3 by calling `resolve()` directly,
#453/#466). The **connect** stays short so a down runtime still fails fast.

If a stream still dies part-way, the agent loop **degrades gracefully** instead of dumping the raw
litellm/aiohttp exception into chat: it keeps whatever answer + activity streamed so far, appends a
short friendly note ("the model stopped responding before the answer was finished…"), **persists**
that partial turn, and ends the stream with `done` — so a reopen still shows it. Only a failure
that produced *nothing* yet ends with `error` (a friendly banner; a non-connection error like
`paused` passes its own text through, which the web keys on for its paused state).

**`embed()` carries the same bound, but enforced differently (#466).** LiteLLM's `ollama`
embeddings dispatch never threads a `timeout=` kwarg through to its HTTP call (unlike the chat
path), so `LlmGateway.embed()` wraps the `litellm.aembedding` call in `asyncio.wait_for(...,
timeout=self._timeout.read)` instead — the same `LLM_TIMEOUT`-derived duration, enforced at the
asyncio level rather than relying on litellm to honor it. Cross-chat recall (ADR-0051) still
layers its own, much shorter (`MEMORY_RECALL_TIMEOUT_S`, default 4s), gracefully-degrading budget
on top via its own `asyncio.wait_for`; this gateway-level guard exists for the direct/module paths
that previously had no bound at all.

### Power (ADR-0005)

| Method · Path | Purpose |
| --- | --- |
| `GET` · `PUT /platform/v1/power` | The main-page power toggle: `paused` unloads models and refuses local inference (`503`); `idle` resumes. |

### Readiness (ADR-0027)

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/readiness?model=…` | A warming snapshot — `{ready, power, components[]}` — folding the power state, module health (compose health), and whether the turn's model is warm (hosted models are always ready). Best-effort: a slow/failing component reports not-yet-ready rather than erroring. The chat stream emits the **same** snapshot as leading `readiness` events so the UI shows a progress bar before the first token. |

### Module registry (ADR-0004/0007)

Each configured base's manifest + health is a **per-base, TTL-cached, single-flight** probe
(#478) — 15s while healthy, 5s while unhealthy (so a recovery shows up promptly) — rather than
a fresh fleet-wide fetch on every call. `_resolve(name)` (the routing path behind tool
invocations, page proxies, `base_url()`, etc.) reads the cache directly and re-probes **only**
that module's own base when its entry is stale; it never fans out to the rest of the fleet, so
one hung or restarting module can no longer delay calls routed to a different, healthy one.
The very first resolve after startup is the one documented exception — it still has to learn
the name→base mapping, so it probes whatever bases it hasn't seen yet. The operator-prefs
overlay (`enabled`/`removed`/`disabled_tools`) is **never** cached — it's read fresh from
Postgres on every call regardless of probe-cache hits, so toggling a module takes effect
immediately. Health changes log a **transition**, not an observation: one WARN the instant a
previously-healthy module goes unreachable (with `repr(exc)`, never the empty string a bare
`TimeoutError` used to stringify to), one INFO the instant it recovers, and DEBUG while a
module has never yet been reachable (the startup/reconcile grace window) — a module that stays
down produces no repeat log.

**The reserved `core` pseudo-module (ADR-0093 §2).** The registry accepts one optional entry
that it answers **in-process** instead of probing over HTTP: the core's own `review` page (see
*Governed playbooks* above). It implements the same surface a real module serves — a manifest,
`GET /pages/{id}`, the review approve/reject, the audit trail — so the registry's handling is a
thin dispatch rather than a parallel implementation, and the shell cannot tell the two apart.
The reserved name is read from the entry's own manifest, so the registry hardcodes nothing.

Crucially it is **not** a configured base URL. `snapshot()` stays exactly 1:1 with the configured
bases (several callers zip the two together), so the pseudo-module can never leak into a
base-driven fan-out: not `enabled_mcp_urls` (it contributes no tools to the agent), not the
re-embed fan-out, not the calendar feed. It opts into a capability by being asked, never by
default — the two reads that *should* see it (`GET /platform/v1/modules`, so the shell discovers
its page; and the pending-suggestions feed) compose it in explicitly. The management writes
—`enabled`, `DELETE`, `suggestions-enabled` — all **403** for it: it is this process, it has no
container, and its review is mandatory (nothing self-applies, ever).

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/modules` | Every configured module: its manifest (tools, events, declared UI), live health, and the operator's `enabled` flag (#126). Disabled modules stay listed so the shell can re-enable them. Served from the probe cache by default; `?refresh=true` forces a fresh fleet-wide re-probe (the Modules page's manual refresh, #478). Also carries the reserved **`core`** pseudo-module (always healthy + enabled — it is this process), so the shell discovers its `review` page like any module's; the Modules screen filters it back out, since it manages what the operator *installed*. |
| `POST /platform/v1/modules/reembed` | Re-embed everything (#332, ADR-0054) — the action behind the Models page's "Re-embed everything" after the embedding model changes. Fans out `POST {base}/reindex` to every healthy, enabled module whose manifest declares `reindexable` (knowledge, notes); returns `{modules: [{module, status}]}` (`started`/`error` per module). Best-effort — one module's failure never aborts the rest. |
| `GET /platform/v1/modules/docker-status` | Whether the core can reach Docker right now (#622, ADR-0099): `{available: bool, reason: str \| null}` — `reason` is the probe's own exception text, surfaced so the Modules page states plainly what's deferred (never "removal disabled" — see the callout below) and how to enable it, without the operator attempting a removal or reading the logs. |
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
| `POST /platform/v1/modules/{name}/tools/{tool}` | Invoke a manifest-declared UI action (runs the module's MCP tool through the host). **403** if the module is disabled. **400** when the tool runs but reports failure — the response `detail` is the tool's own error message, so the shell can show it instead of closing the form as a success (#435). **502** `{name} action failed: module unreachable` when the module refuses the connection or does not answer within the call timeout (30s) — the MCP dispatch is bounded and its transport failure mapped to a controlled status, so a down/restarting module no longer surfaces as a raw `NetworkError` (#472). |
| `GET /platform/v1/modules/{name}/status` | Proxy the module's `ui.status_url` endpoint (returns the module's live status JSON as-is). 404 if the module is unreachable or has no `status_url`. |
| `GET /platform/v1/modules/{name}/read?path=…` | Proxy an **editor** module's `GET /read` text-file endpoint for its split-screen reader (knowledge, notes): `{path, name, content}`. Upstream 4xx pass through (415 binary, 413 too large, 404 missing); an unreachable module is a controlled **502**. (The unified **Files** read is core-owned at `GET /platform/v1/files/read` — ADR-0063; see [file space](../reference/files.md).) |
| `POST /platform/v1/modules/{name}/pages/{page_id}/project?project=…` | Create a new knowledge base (project / top-level scope) in an editor page's store (#KB-refactor). 409 if it exists, 400 for an invalid name; the module enforces name-safety. |
| `POST /platform/v1/modules/{name}/pages/{page_id}/suggestions/{id}/approve` | Approve a staged suggestion — the module applies + indexes it (#220, ADR-0033). Optional `{content}` body is the operator's **edited draft** (ADR-0090 — a free-form edit, a per-hunk merge, or both), forwarded so what's written is what was actually approved; absent ⇒ apply the module's proposal unedited. Operator-only. Records a row in the module's audit trail before dropping the pending suggestion. |
| `POST /platform/v1/modules/{name}/pages/{page_id}/suggestions/{id}/reject` | Reject a staged suggestion — the module discards it, nothing written (#220). Operator-only. Also records an audit row (ADR-0090). |
| `GET /platform/v1/modules/{name}/pages/{page_id}/audit?limit=` | The resolved-decision **audit trail** for a `review` page (ADR-0090): what the module proposed vs. what the operator actually approved (or that it was rejected), newest first. `limit` defaults to 50 (1–200). Same 404 gate as approve/reject (only a `review` page exposes it). |
| `GET /platform/v1/suggestions` | **Cross-module pending-suggestions feed** (#KB-refactor): every enabled module with a `review` page — the knowledge base **and** private **notes** — each item tagged with `module` + `page_id`. `operation` ∈ `create`/`update`/`append`/`delete`/`move`/`mkdir`/`mkproject` (`append` is notes-only — the agent supplies just the text to add). Best-effort aggregation — a down / disabled / erroring module is skipped, not fatal. Backs the chat composer's suggestion bubble and the Suggestions page. (Lives at `/platform/v1/suggestions`, not under `/modules`.) |
| `GET /platform/v1/calendar-feed?start=&end=` | **Cross-module calendar-feed aggregate** (#469, ADR-0088): date-anchored items (e.g. open tasks with a due date) from every enabled, healthy module — each stamped with its owning `module`. **Not a manifest-declared capability** — probes every module for `GET {base}/calendar-feed?start=&end=` and skips it on a 404/unreachable, the same best-effort tolerance `/suggestions` already relies on, so a module opts in purely by serving the path (`tasks` is the first). Item shape: `{id, title, date, status, ref_id, kind}` (`date` a floating `YYYY-MM-DD`, `end` exclusive — ADR-0023's own range convention; `kind` + `ref_id` + the stamped `module` route a click to that module's existing `GET /resolve/{kind}/{ref_id}` hover-card, ADR-0019 — no new UI contract). Backs the calendar page's read-only task-due-date overlay. (Lives at `/platform/v1/calendar-feed`, not under `/modules`.) |

> **Privileged surface, opt-in (ADR-0028, #307, #382, #622/ADR-0099).** Tearing down a removed
> module's container — and applying the Ollama KV-cache type — needs the Docker socket. The core
> touches it through a single `DockerController`: it stops/removes **only a configured module's
> own container**, and separately **restarts only an allowlisted infra container** (`ollama`,
> which is never removable). Both are scoped to this Compose project and never touch core-app /
> web / a data-plane service. Module **removal itself never needs the socket** (#382): it
> tombstones the module (hidden + unrouted at once) regardless, and **defers** the container
> teardown to the next startup reconcile when Docker isn't reachable — so removal always works;
> a KV-cache change likewise saves without applying. **The socket is NOT mounted by default**
> (#622, ADR-0099) — mounting it unconditionally bought nothing real anyway, since the app's
> unprivileged uid (10001, the same [entrypoint privilege drop](../infrastructure/index.md#shared-file-space)
> the shared file space uses) can't reach it without a host-matched group either way.
> Opt in with `services/core-app/compose.docker-socket.yaml` (mounts the socket **and** forwards
> `DOCKER_GID`, the host's docker-socket group id — the entrypoint joins it before dropping
> privileges); see [Docker-socket access](../infrastructure/index.md#docker-socket-access-opt-in-622).
> `GET /platform/v1/modules/docker-status` reports the live state so the Modules page states it
> proactively instead of an operator finding out by attempting a removal.

Caller-supplied path segments the registry interpolates into a module request —
`ref_id`, entity `kind`, `page_id` — reject `/`, `\`, or `..` with **400** so a
supplied id cannot redirect the outbound request on the module host (#175).

Every module-proxy GET (status, docs, pages, resolve, attachments, accounts) maps an
upstream failure to a **controlled** status, not an unhandled exception (#209): a module's
client error (4xx) passes through as-is (e.g. a missing entity stays a `404`), while a 5xx,
a timeout, or a connection failure becomes a `502` carrying the operation — so a slow or
erroring module can no longer surface as an opaque **Bad Gateway** to the shell.

The **tool-invocation POST** (the board/calendar UI actions above) is held to the same
guarantee (#472). Its dispatch runs over MCP rather than a plain HTTP proxy, so the host
(`McpHost.call`) bounds every hop — connect, `initialize`, and the tool RPC — with a 30s
timeout and normalizes a refused/dropped connection or an RPC read timeout (which the
streamable-HTTP client's anyio task group raises **wrapped in an `ExceptionGroup`**) into a
single `ModuleUnreachableError`. `ModuleRegistry.invoke` maps that to the **502** above; a
tool that *ran* and reported failure stays a **400** with its own message (`ToolCallError`,
#435). The two are kept distinct on purpose — "the module never answered" vs. "the tool
rejected the request".

### Chat bridges (ADR-0062)

The connect/manage surface behind the web shell's **Settings → Chat bridges** (#369). The core
owns connecting a bridge because the browser must never hold a token (constraint #6) and a
module is stateless w.r.t. identity (constraint #4): it writes the per-tenant bot token to
OpenBao (`messaging/<bridge>` → `{token, enabled}`) and then calls the [messaging](messaging.md)
module's reload control path so the bridge connects at runtime — no restart.

| Endpoint | Purpose |
| --- | --- |
| `GET /platform/v1/messaging/bridges` | List every bridge + its [`BridgeStatus`](../reference/messaging.md#bridgestatus) (proxied from the module's `/status`). |
| `PUT /platform/v1/messaging/bridges/{bridge}/token` | **Connect**: store the write-only bot token in OpenBao and reload the bridge (`{token}`). **404** unknown/unmanageable bridge, **400** blank token. |
| `POST /platform/v1/messaging/bridges/{bridge}/enabled` | **On/off** without forgetting the token (`{enabled}`); **400** if no token is stored yet. |
| `DELETE /platform/v1/messaging/bridges/{bridge}` | **Disconnect**: clear the token from OpenBao and reload (idempotent). |

### Maintenance orchestrator (ADR-0060)

One coordinated batch over the core's background jobs, behind a single trigger (#383). The jobs are
a small **registry** — a `MaintenanceJob` is a labelled async unit of work — so a new job type
registers by being added to the list; the run / route / schedule machinery is unchanged. Four
ship: the **memory fact-extraction drain** (light, nightly-eligible — drains the
deferred-extraction queue, ADR-0051), the **standing-profile synthesis** (light, nightly-eligible
— `ProfileSynthesizer.run` distils each tenant's facts into its statically-injected profile,
ADR-0094), the **module re-index** fan-out (heavy, manual-only — the same `reembed` fan-out as
above), and **memory facts re-embed** (heavy, manual-only — calls `UserFactStore.reembed_all` for
the default tenant, #436). Jobs run **sequenced** (gentle on a single GPU) and each is contained:
one job's failure becomes an `error` result, never aborting the rest. Nightly auto-runs follow a
runtime-editable **schedule** (below, #621); the manual "run everything" trigger is always
available regardless of it.

A batch runs as a **detached background task**, decoupled from the request that started it (#561)
— the same shape as chat turns (`agent/live_runs.py`, #376). `POST /run` starts it and returns
immediately; the orchestrator tracks a **current run** with live `pending`/`running`/`ok`/
`skipped`/`error` status per job as it sequences, exposed by `GET` alongside the last *completed*
run. A second `POST` (or an overlapping nightly window) while one is in flight doesn't start a
competing batch — it 409s, carrying nothing but a message, and the caller re-`GET`s to observe/join
the run already going. `MaintenanceOrchestrator.shutdown()` cancels an in-flight batch cleanly at
app shutdown (marking whatever hadn't finished `error`) rather than orphaning it against
infra that's about to close.

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/maintenance` | `{schedule_enabled, schedule_cadence, schedule_hour, schedule_weekday, next_run_at, jobs:[{key,label,nightly}], last_run, current_run}` — the registered jobs, the *effective* schedule (the tenant's own override, else the env-configured default), an ISO `next_run_at` estimate (`null` when disabled — a display estimate only; the scheduler's own due-check additionally avoids re-firing within an already-run window), the last *completed* run (or `null`), and the in-flight run (or `null`) with its live per-job progress. |
| `PUT /platform/v1/maintenance/schedule` | Set the tenant's schedule — body `{enabled, cadence: "hourly"\|"daily"\|"weekly", hour: 0-23, weekday: 0-6\|null}` (#621). Validated as a whole (**400** on an invalid shape — an unknown cadence, an out-of-range hour, a `weekly` with no/bad weekday, or a weekday given for a non-weekly cadence) before it persists; returns the full refreshed `GET` shape. |
| `POST /platform/v1/maintenance/run` | **202** — starts every job now (`scope: "all"`) as a background task and returns its live progress immediately: `MaintenanceCurrentRun` `{started_at, scope, jobs:[{key,label,status,detail}]}` (`status` ∈ `pending`/`running`/`ok`/`skipped`/`error`). **409** if a batch is already running — the body is a plain `{detail}` message; re-`GET` for the in-flight run. |

The **manual** trigger (the web **Settings → Maintenance** card) is always available and runs all
jobs regardless of the schedule; the card rehydrates onto `current_run` on mount and polls a few
seconds apart while one is live, so a page refresh mid-batch lands back on the same run instead of
losing it.

**The nightly schedule is a real, per-tenant, runtime-editable trigger (#621, ADR-0098)** —
enable/disable, an `hourly`/`daily`/`weekly` cadence, an hour, and (weekly only) a weekday,
interpreted in the tenant's timezone (ADR-0039). It governs the orchestrator **as a whole**
(every `nightly=True` job runs together, never a per-job schedule — the job registry above stays
untouched and additive-only, so #615's incoming reflection job keeps riding this one shared hour
per ADR-0093). Persisted per tenant in `maintenance_schedule_prefs` (`MaintenanceScheduleStore`,
the same settings-primitives shape as `timezone_prefs`/`page_order_prefs`); a tenant that has
never `PUT` one falls back to the env-configured default (`MAINTENANCE_SCHEDULE_ENABLED`/
`MAINTENANCE_HOUR`, `cadence="daily"`) — a fresh install behaves exactly as it did before this
existed. `run_periodic` is a plain poll (`MAINTENANCE_POLL_INTERVAL_S`, default 60s) that re-reads
the current schedule fresh every tick — not a single `sleep_until_hour` computed once at wake,
since a schedule editable at runtime could change while that sleep was in progress. Due-ness
(`is_due`) and the panel's next-run estimate (`next_run_at`) are pure functions of the schedule
and the current local time; the "last fired" bookkeeping that dedupes a window is in-memory only
(a restart re-evaluates fresh against the wall clock, same as before). Consolidating the
per-runner nightly schedules onto this orchestrator remains the named follow-up. Every *completed*
run publishes a tenant-scoped `maintenance.completed`; a run interrupted by shutdown is discarded,
not published.

### Scheduled turns (ADR-0092)

Recurring prompts that run **unattended** and deliver into their own chat session — the
time-driven half of proactivity (the event-driven half, listeners/alerts, is a later
milestone). An operator authors a prompt, a cadence (daily/weekly at a local hour), and it
fires on its own with no HTTP caller — the same headless-turn shape the inbound messaging
consumer above already uses for a bridge message (`Agent.run(tenant_id=..., session_id=...)`,
no SSE).

`ScheduledTurnScheduler` is a **single poll loop** (`SCHEDULED_TURNS_POLL_INTERVAL_S`, default
60s), not one task per row: each row carries its own independently configured hour (and, for a
weekly cadence, weekday) and rows are created/paused/deleted at runtime, which the existing
single-hour `sleep_until_hour` primitive (shared by the extraction drain and the maintenance
orchestrator above) can't express. Each tick reads every enabled row, resolves the operator's
timezone the same way those two do, and runs every due row **sequentially** (gentle on one
local GPU). A row fires once per matching window — `last_run_at` (set on a real run *and* a
paused-skip) is compared by local calendar date so a tick landing anywhere inside the target
hour doesn't re-fire on the next poll.

**Delivery is an ordinary session, not a new persistence path.** A fresh session id
(`scheduled-<uuid>`) is minted when the turn is created; the session comes into being — with a
title derived from its first message (the prompt itself) — the moment it first fires, exactly
like any other session. Metering is automatic: threading the row's real tenant through
`Agent.run` means the usage event attributes to it with no extra wiring (constraint #1/#8).

**Power state**: the poll loop itself is pause-agnostic; the per-row runner checks
`power.paused` right before invoking the agent and, if paused, records the skip
(`last_status = "skipped (paused)"`, advancing `last_run_at`) rather than running — skip and
record once per window, never a burst of catch-up runs when the operator resumes.

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/scheduled-turns` | The tenant's scheduled turns, oldest first. |
| `POST /platform/v1/scheduled-turns` | Create one: `{prompt, cadence: "daily"\|"weekly", hour, weekday?}` (`weekday`, 0=Monday..6=Sunday, required for `"weekly"`). **400** on a blank prompt, an out-of-range hour/weekday, or a missing weekday for a weekly cadence. Mints a fresh `delivery_target` session id. |
| `POST /platform/v1/scheduled-turns/{id}/enabled` | Pause/resume: `{enabled}`. **404** unknown id. |
| `DELETE /platform/v1/scheduled-turns/{id}` | Remove it. **204**; **404** unknown id. |

Settings-surface only (ADR-0018): shell-rendered (the web **Settings → Scheduled turns**
card), not a module page — the feature lives entirely in the core, so there is no module UI
to gate it behind. Single-runner v1: one core instance evaluates the poll loop; a multi-instance
SaaS deployment needs leader election or a distributed queue so two instances can't double-fire
the same row — a named follow-up, not attempted here.

### Events (NATS)

Emits **`<tenant>.llm.usage`** after every inference call — model, token counts, latency.
No prompt/response content, no keys. Feeds observability now and SaaS metering later.

**Inbound messaging consumer (ADR-0058)** — the first *inbound* NATS subscriber in core (the
foundation for Phase 4 chat bridges). It **consumes `<tenant>.messaging.inbound`**
([`InboundMessage`](../reference/messaging.md#inboundmessage)), maps the channel to a session
id (`<bridge>:<channel>[:<thread>]`), runs a **headless** agent turn (the same `Agent.run` the
web uses — no SSE; the answer is collected and persisted like any turn), and **emits
`<tenant>.messaging.outbound`** ([`OutboundMessage`](../reference/messaging.md#outboundmessage))
for the [messaging](messaging.md) module to deliver. It respects power state (paused → skip,
the user resends once resumed) and contains every failure (a bad payload or failed turn is
logged and dropped). v1 subscribes under the default tenant; multi-tenant fan-out (a wildcard
or per-tenant subscriptions) is the named follow-up. Gated by `MESSAGING_INBOUND_ENABLED`.
Emits **`<tenant>.maintenance.completed`** after each maintenance batch (ADR-0060) — the run's
`{ran_at, scope, jobs:[{key, status, detail}]}` summary, for downstream consumers.

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
| `MESSAGING_INBOUND_ENABLED` | `true` | Run the inbound-messaging consumer (chat bridges, ADR-0058). |
| `MESSAGING_MODEL` | — | Optional dedicated model for bridge turns; blank = the default chat model. |
| `ASK_USER_TTL_HOURS` | `24` | How long a turn paused by `ask_user` waits for an answer before its suspended run is reaped (ADR-0053). |
| `DRAFT_REVIEW_TTL_HOURS` | `24` | How long a turn paused on a draft-first send waits for Confirm/Decline before its pending draft is reaped (ADR-0085, #563). |
| `LIVE_RUN_GRACE_SECONDS` | `300` | How long a *finished* in-flight run stays re-attachable in memory before it is reaped (ADR-0055). Pure cache — the answer is already durable, so this only bounds how long a late re-attach can tail the buffer. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Conversation persistence. |
| `QDRANT_URL` | `http://qdrant:6333` | Semantic-recall vectors. |
| `MEMORY_EMBED_MODEL` | `nomic-embed-text` | Local embedding model for recall. |
| `MEMORY_EXTRACTION_MODE` | `nightly` | When fact extraction runs: `nightly` (deferred to a queue drained off-hours, ADR-0051) or `immediate` (a background task after each turn, ADR-0045). |
| `MEMORY_EXTRACTION_HOUR` | `3` | Local hour (0-23) of the nightly drain, in the operator's timezone. |
| `MEMORY_EXTRACTION_MODEL` | — | Optional small dedicated model for the extraction call (e.g. `llama3.2:3b`); blank = the default chat model. |
| `MEMORY_EXTRACTION_BATCH_LIMIT` | `200` | Max exchanges distilled per nightly drain. |
| `MEMORY_RECALL_TIMEOUT_S` | `4.0` | Time-box (seconds) for the inline recall embed before a turn proceeds without it (ADR-0051). 4s (was 2s) fits a single-GPU embed-model swap. |
| `MEMORY_PROFILE_MODEL` | `""` | Optional dedicated model for the nightly **standing-profile** synthesis (ADR-0094); blank = the operator's default chat model. A small model keeps the pass cheap. |
| `MEMORY_PROFILE_MAX_VERSIONS` | `5` | How many past standing-profile versions to retain per tenant (the newest is injected). |
| `DEFAULT_TIMEZONE` | `UTC` | Fallback IANA timezone for the `now` tool when unset in Settings (ADR-0039). |
| `MAINTENANCE_SCHEDULE_ENABLED` | `false` | Run the maintenance orchestrator's **nightly** batch (ADR-0060). Off by default — the manual trigger is always available; this opts into a coordinated nightly light batch. |
| `MAINTENANCE_HOUR` | `4` | Local hour of the scheduled nightly maintenance batch, an hour after `MEMORY_EXTRACTION_HOUR`. |
| `SCHEDULED_TURNS_POLL_INTERVAL_S` | `60` | How often the scheduled-turns poll loop checks for a due row (ADR-0092). |
| `OTEL_TRACES_ENABLED` | `false` | Emit OpenTelemetry traces — the agent loop, platform API, and event bus — to Tempo (#57). See the [tracing reference](../reference/observability.md#tracing-57-adr-0068). |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://tempo:4318` | OTLP/HTTP base URL for traces (the exporter appends `/v1/traces`). |

Provider keys are **not** configured here — they go through the UI into OpenBao.

## Data model

- **Postgres `agent_messages`** — conversation history (append-only in normal use; the last
  turn can be edited/truncated for regenerate/edit, #302): `id`, `tenant`,
  `session_id`, `role`, `content`, `created_at`, plus JSON `entity_refs` / `attachments`
  (ADR-0019) and `activity` — the assistant turn's persisted process, rendered as the folded
  activity timeline on reopen (ADR-0041). `activity.timeline` is the **chronological**
  interleaving of thinking blocks and tool steps (think → call → think, #300); the flat
  `thinking`/`steps` are derived and kept for backward compatibility (older rows have only
  those). Tenant-scoped; post-release columns are added in place at startup (no migration). The
  `memory_search` built-in's *sessions* half (ADR-0089) runs a tenant-scoped case-insensitive
  content match here (portable `ILIKE`, no full-text index — a single operator's history is
  small; FTS is a future optimization), joined back to each session's opening-message title.
- **Postgres `agent_attachments`** — the core-side handle for an uploaded chat attachment
  (ADR-0019): `att_id` (primary key), `tenant`, `kind` (the upload's MIME content-type), `title`,
  `content` (raw bytes), `created_at`. Written by `POST /agent/attachments`; read back once per
  turn by the attachment expander (`AttachmentStore.get`), scoped to the requesting tenant.
  `kind.startswith("image/")` is what routes a `file` attachment to the vision path instead of
  text expansion (#633) — see **Agent** above.
- **Postgres `llm_prefs`** — per-tenant operator preferences: `global_default` (chat model),
  `global_embed_default` (embedding model, #214), `context_window` (global `num_ctx`),
  `kv_cache_type` (Ollama KV-cache, ADR-0046), `agent_max_steps` (agent loop bound, #297),
  `hidden_models` (JSON list). A missing row means all defaults are `null` (fall back to env
  settings).
- **Postgres `model_settings`** — per-`(tenant, model)` tuning (ADR-0044/0045):
  `context_window`, `keep_alive`, and `device` (`"gpu"`/`"cpu"`/`null`), all nullable
  (`null` = inherit). Drives the per-model resolution chain in the gateway (see **Per-model
  settings**). A missing row means the model inherits the global pref / env defaults.
- **Postgres `saved_models`** — per-`(tenant, model)` saved **hosted**-model ids (#496):
  `tenant`, `model`, `added_at` (epoch-ms, `BigInteger`, drives most-recent-first ordering).
  Only hosted ids land here — a known `<provider>/` prefix; the route rejects locals so an
  `hf.co/…` model can't masquerade as hosted. A durable, cross-device home for the strings
  entered in the chat picker (the browser's `recentModels` is only a warm cache).
- **Postgres `module_prefs`** — per-`(tenant, module)` operator preferences: `enabled`
  holds the enable/disable flag (#126), `removed` tombstones a module after its container is
  deleted (#127), `models` holds per-slot model choices (#128), `disabled_tools` holds a JSON
  list of tool names the operator has toggled off (#213), `collections` holds the
  account/collection selection (`{enabled, active}` JSON, ADR-0030), and `suggestions_enabled`
  holds the per-module review on/off toggle (#KB-refactor; NULL ⇒ on). A module with no row
  defaults to enabled, not-removed, core-default models, all tools on, review on, and the local
  default collection. Post-release columns are added in place at startup (no migration framework).
- **Postgres `core_files`** — the core-owned **file index** over the swappable `FileStore`
  (ADR-0063): a tenant-scoped catalogue of the file-space tree (`path`, `name`, `size`, `mtime`,
  `kind`), built by the startup scan and kept current by the `FILES_WATCH` watcher; it backs the
  unified **Files** page and search. The operator Files doors keep it in step immediately — an
  **upload** upserts the entry, a **move** re-paths it, and a **delete** (#564) removes the entry
  and its subtree (`FileIndex.remove_subtree`) — so a change shows in search/listing at once, with
  the watcher as the backstop. Storage-module objects are merged in at request time, not stored
  here — a node reported by both sources collapses to one row, the file-space entry winning so its
  movability stays authoritative (#560; see [file space](../reference/files.md)).
- **Postgres `timezone_prefs`** — per-tenant IANA timezone for the `now` tool (ADR-0039):
  `tenant`, `timezone`. A missing row (or null) falls back to `DEFAULT_TIMEZONE`.
- **Postgres `page_order_prefs`** — per-tenant left-nav page order (#543): `tenant`,
  `order_json` (a JSON list of page paths, most-preferred-first). A missing row (or null)
  falls back to the manifest-declared default order; opaque storage only — merge semantics
  live client-side (ADR-0018), not in this table.
- **Postgres `maintenance_schedule_prefs`** — per-tenant maintenance-orchestrator schedule
  (#621, ADR-0098): `tenant`, `enabled`, `cadence` (`hourly`/`daily`/`weekly`), `hour` (0-23),
  `weekday` (0=Monday..6=Sunday, nullable — weekly only). A missing row falls back to the
  env-configured default (`MAINTENANCE_SCHEDULE_ENABLED`/`MAINTENANCE_HOUR`, `cadence="daily"`);
  once set, the row is authoritative for every field at once. See **Maintenance orchestrator**
  above.
- **Postgres `agent_instructions`** — per-tenant editable base system prompt (#497, ADR-0083):
  `tenant`, `instructions` (nullable). A NULL/blank row falls back to the shipped
  `DEFAULT_AGENT_INSTRUCTIONS`; resolved per turn and injected first in `Agent._assemble`.
- **Postgres `agent_instructions_versions`** — snapshots of the base prompt (ADR-0046 via
  ADR-0093 §3): `id`, `vid`, `tenant`, `content`, `created_at`. Each `set_instructions` records the
  prompt it **replaced** (the first edit therefore captures the shipped default), deduplicated,
  newest `MAX_VERSIONS` (50) per tenant retained, oldest pruned. A parallel table to
  `agent_playbook_versions` rather than one shared version stream: the base prompt is a per-tenant
  singleton and a playbook is one of N named documents, so interleaving them would complicate
  "roll back *this* document".
- **Postgres `agent_playbooks`** — named blocks of guidance composed onto the base prompt
  (ADR-0093 §3): `id` (uuid), `tenant`, `name` (unique per tenant), `content`, `enabled`,
  `created_at`, `updated_at`. Only **enabled** rows are composed into the turn's prompt, oldest
  first then by name (a total, stable order — the primary key is a uuid and carries none).
- **Postgres `agent_playbook_versions`** — snapshots of a playbook's content (ADR-0046): `id`,
  `vid`, `tenant`, `playbook_id`, `name` (snapshotted too, so a version stays readable after a
  rename), `content`, `created_at`. Same replace-then-snapshot rule, dedup, and 50-per-playbook
  cap as the base prompt above. Dropped with its playbook.
- **Postgres `agent_playbook_proposals`** — the reserved `core` review page's **pending queue**
  (ADR-0093 §2): `id`, `sid`, `tenant`, `path` (`instructions`, or `playbooks/<name>`),
  `operation` (`update`/`create` only — the agent never proposes a delete), `proposed_content`,
  `origin`, `note`, `created_at`. The queue *is* the set of rows (ADR-0033): resolving one drops
  it. Written **only** by the nightly reflection pass; read by the review page.
- **Postgres `agent_playbook_decisions`** — the durable resolved-decision trail behind that queue
  (ADR-0090): `id`, `sid`, `tenant`, `path`, `operation`, `origin`, `note`, `proposed_content`,
  `applied_content` (empty for a reject — the operator's edit is the delta worth keeping),
  `decision` (`approved`/`rejected`), `proposed_at`, `decided_at`. Newest `MAX_DECISIONS` (200)
  per tenant retained. Recorded **before** the pending row drops, so a crash between the two
  leaves an audited decision and a re-resolvable queue row rather than a silently vanished
  proposal. The `rejected` rows are what the reflection pass reads back as negative context
  (ADR-0093 §6).
- **Postgres `agent_suspended_runs`** — a turn paused by `ask_user` (ADR-0053): `id` (run_id),
  `tenant`, `session_id`, `model`, `pending_call_id`, `question`, `conversation` (JSON),
  `created_at`. Written on suspend, **consumed** on resume, reaped after `ASK_USER_TTL_HOURS`.
- **Postgres `agent_pending_drafts`** — a turn paused on a draft-first send (ADR-0085, #563):
  `id` (run_id), `tenant`, `session_id`, `model`, `pending_call_id`, `tool`, `module`, `summary`,
  `draft` (JSON — the composed message), `conversation` (JSON), `created_at`. A **sibling** of
  `agent_suspended_runs` (a separate table, so `create_all` builds it with no migration and the two
  consume-on-resume paths can't cross). Written on suspend, **consumed** on Confirm/Decline, reaped
  after `DRAFT_REVIEW_TTL_HOURS`.
- **Postgres `scheduled_turns`** — recurring prompts that run unattended (ADR-0092): `id`,
  `tenant`, `prompt`, `cadence` (`daily`/`weekly`), `hour`, `weekday` (nullable, weekly-only,
  0=Monday..6=Sunday), `delivery_target` (the session id the turn delivers into), `enabled`,
  `created_at`, `last_run_at`, `last_status`. `last_run_at` is set on both a real run and a
  paused-skip, so the scheduler's poll tick evaluates a row's due-ness at most once per
  matching window.
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
  longer written; any existing vectors are simply unused.) The collection is created at
  whatever dimension the embedder had on first use; `UserFactStore._ensure` checks that dim
  against the current embedder on each process's first touch and **reconciles a mismatch**
  in place — re-embedding every stored fact's text and recreating the collection at the new
  size, preserving each fact's id and metadata — rather than silently 400ing on every
  recall/save the way it did before #436. The reconcile pages through the collection (via
  Qdrant's scroll offset) until every point has been visited, so it never drops facts beyond
  a bounded scan window regardless of corpus size (#450, ADR-0076).
- **Postgres `memory_extraction_queue`** — finished exchanges awaiting background fact
  extraction (ADR-0051): `id`, `tenant`, `user_text`, `assistant_text`, `created_at`. In the
  default **nightly** mode the agent enqueues each exchange here instead of distilling it inline;
  the `ExtractionRunner` drains it once a day (at `MEMORY_EXTRACTION_HOUR` in the operator's
  timezone), serially, so extraction never competes with a live turn for the GPU. Drained rows
  are deleted; because the queue is durable, a restart never loses a pending exchange.
- **Postgres `standing_profiles`** — the compact per-tenant **standing profile** the agent injects
  each turn (#527, ADR-0094): `id`, `tenant`, `content`, `source` (`auto` | `edited`), `created_at`.
  Append-only and **versioned** — each write keeps the last `MEMORY_PROFILE_MAX_VERSIONS` (5) per
  tenant, newest injected (the ADR-0046 snapshot idiom). Synthesized on the nightly **maintenance
  batch** (`profile_synthesis_job`, ADR-0060) from the fact store via one gateway call, and injected
  **statically** in `_assemble` with no turn-time embed — moving the common-case recall cost off the
  response path (the ADR-0051 trade, now for the profile). An operator edit is stored `edited` and
  **pinned**: synthesis skips a tenant whose current profile is `edited`, so a correction survives
  re-synthesis until the operator clears it.

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
