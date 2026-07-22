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

## `GET /platform/v1/page-order` · `PUT /platform/v1/page-order`

The operator's drag-and-drop order for left-nav module pages (#543) — purely a shell/nav
concern (ADR-0018), no module ever reads or writes it. `GET` returns `{order: string[]}`, each
entry a page's `path` (e.g. `/m/calendar/main`), most-preferred-first; `[]` means no
preference is set and the nav falls back to its manifest-declared (`nav_order`-then-label)
default. `PUT {order}` replaces the stored list wholesale — no validation against the current
module set, since merge semantics (unknown ids append, stale ids are ignored) are resolved
client-side, not here (`sortByPageOrder` in `src/app/registry.ts`). Both take an optional
`tenant_id` query param, falling back to the default tenant when omitted. Edited from the web
**Modules** screen's **Page order** card, never the sidebar itself.

---

## `GET` · `POST` · `DELETE /platform/v1/llm/saved-models`

The operator's saved hosted-model ids (#496) — a tenant-scoped, durable home for the hosted
model strings entered in the chat picker, so they survive restarts / a PWA reinstall and follow
the tenant across devices (unlike the browser's per-origin `recentModels` localStorage cache).

- **`GET`** → `{models: [{model, provider, context_length, capabilities}]}`, most-recently-saved
  first. `provider` is the id's `<provider>/` prefix (for grouping on the Models page).
  `context_length`/`capabilities` (#618) come from LiteLLM's own model-cost map — the same source
  `/models/details` uses for a hosted id — always included (a static lookup, never a network
  call); `null`/empty when the model isn't in that map, never a fake default.
- **`POST {model}`** persists one id, idempotent — an **atomic upsert** (a re-save bumps it to the
  front; two concurrent first-saves of the same id can't race between the read and the write to a
  500). **400**s anything that isn't a *hosted* id — a known `<provider>/` prefix followed by a
  **non-empty model part** (`claude/…`, `gpt/…`, …) — so a local `hf.co/org/model:tag`, **or a
  provider-only `claude/` with no model**, can never land here (the server-side half of the fix for
  the web client's old `includes("/")` misclassification).
- **`DELETE ?model=…`** (query param, since ids carry `/` and `:`) forgets one; a no-op if absent.
  Removing the id that is the current `llm_prefs.global_default` is left as-is by design — the
  default keeps pointing at it (still valid for inference, just no longer *listed*); change or clear
  the default separately via `PUT …/prefs/default`.

Backed by the tenant-scoped `saved_models` table. The chat picker renders this list (auto-saving
on use), the Models page lists it (remove / set-as-default), and module model slots offer it
(ADR-0029). Mutations **503** when the store is unavailable.

## `GET /platform/v1/llm/catalog` · `GET /platform/v1/llm/catalog/variants`

The browsable model catalog the core parses from an upstream library on a schedule (#269), and
the on-demand quant-variant lookup that complements it (#330). Shell-facing; global, not
tenant-scoped (both mirror a public registry).

- **`GET /llm/catalog`** → `{entries, source, updated_at, stale}`, the cached snapshot (never
  blocks on the network). Each entry is
  `{id, family, params, size_gb, description, tags, pulls}`:
  `id` is the pullable ref (`llama3.1:8b`, or the bare family for a size-less model); `size_gb`
  is the **real on-disk size** backfilled from the family's tags page (#571) — `null` until the
  background size fill or a variant lookup reaches the family, and always `null` for cloud rows;
  `tags` is a loose string array from the parser's vocabulary (`general`, `code`,
  `multilingual`, `vision`, `tools`, `thinking`, `embedding`, `small`, `cloud`) — unknown future
  tags must be ignored, not rejected. A `cloud` tag marks a **cloud-only** model: no local
  weights (its only upstream tag is a cloud alias) — the UI badges it, offers no Pull, and skips
  fit. `stale` flags a seed / last-good snapshot after a failed or skipped refresh.
- **`GET /llm/catalog/variants?model=…`** → `{model, variants: [{tag, quant, size_gb}]}`, the
  pullable quantizations of the given model (`model` is a query param — names carry `:`).
  `quant` is the parsed quant label (`q8_0`, `fp16`, … — `""` for the default build) and
  `size_gb` the tag row's real on-disk size (#571; `null` when upstream shows none, e.g. a
  cloud alias). Best-effort: any failure returns an empty list. A successful lookup also folds
  the family's sizes into the catalog snapshot (the same per-family cache feeds both).

---

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

## `GET /platform/v1/maintenance` · `PUT .../schedule` · `POST .../run`

The maintenance orchestrator (ADR-0060) — one coordinated batch over the core's background jobs
(memory fact-extraction drain, module re-index fan-out). `GET` returns
`{schedule_enabled, schedule_cadence, schedule_hour, schedule_weekday, next_run_at,
jobs:[{key,label,nightly}], last_run, current_run}` — the registered jobs, the tenant's
*effective* schedule (its own override, else the env-configured default), an ISO `next_run_at`
estimate (`null` when disabled), the last *completed* run (or `null`), and any run **in flight**
(or `null`).

`PUT /schedule` sets the tenant's schedule (#621, ADR-0098) — body
`{enabled: bool, cadence: "hourly"|"daily"|"weekly", hour: 0-23, weekday: 0-6|null}`. Validated as
a whole before it persists: **400** on an unknown cadence, an out-of-range hour, a `"weekly"`
cadence with no/invalid `weekday`, or a `weekday` given for a non-weekly cadence. On success
returns the full refreshed `GET` shape. The schedule governs the orchestrator's `nightly` jobs
**collectively** — there is no per-job schedule.

`POST /run` **starts** every job now (`scope: "all"`) as a background task and returns
**immediately** — it does not wait for the batch, which can take minutes (#561). On success it's
**202** with the just-started `MaintenanceCurrentRun`:
`{started_at, scope, jobs:[{key,label,status,detail}]}`, where `status` is
`pending`/`running`/`ok`/`skipped`/`error` per job (`pending`/`running` only ever appear here, live;
a *completed* run's jobs — `last_run`, and the `maintenance.completed` event — are always
`ok`/`skipped`/`error`). If a batch is **already running**, `POST /run` responds **409** instead of
starting a second one — the body is a plain `{detail}` message, not a run; the caller re-`GET`s
`/platform/v1/maintenance` to observe/join the in-flight run via `current_run`. This also covers an
overlapping scheduled window: the scheduled run is skipped (logged, not an error) rather than
racing the manual trigger.

A tenant-scoped `maintenance.completed` NATS event carries the completed run's summary — a batch
interrupted by app shutdown is discarded, not published. Driven by the web **Settings →
Maintenance** card: it rehydrates onto `current_run` on mount (a refresh mid-batch lands back on
the same run) and polls a few seconds apart while one is live. The schedule (enable/disable +
cadence + hour/weekday) is a poll loop (`MAINTENANCE_POLL_INTERVAL_S`, default 60s) that re-reads
the tenant's current schedule every tick — not a single sleep computed once, since the schedule is
now editable at runtime via `PUT`.

---

## Scheduled turns (ADR-0092)

Recurring prompts that run unattended and deliver into their own chat session — a
Settings-surface CRUD (list/create/pause/delete), not a module page (ADR-0018).

### `GET /platform/v1/scheduled-turns`

The tenant's scheduled turns, oldest first: each as
`{id, prompt, cadence, hour, weekday, delivery_target, enabled, created_at, last_run_at,
last_status}`. `cadence` is `"daily"` or `"weekly"`; `weekday` (0=Monday..6=Sunday) is only
meaningful for `"weekly"`. `last_status` is `"ok"`, `"skipped (paused)"`, or an `"error: …"`
string; both `last_run_at`/`last_status` are `null` until the turn has fired once.

### `POST /platform/v1/scheduled-turns`

Create one: `{prompt, cadence, hour, weekday?}`. **400** on a blank prompt, an hour outside
0-23, an unknown cadence, or a `"weekly"` cadence with no (or an out-of-range) `weekday`.
Mints a fresh session id (`scheduled-<uuid>`) as `delivery_target` — the session comes into
being, titled from the prompt itself, the moment the turn first fires; there is no separate
"create session" step or picker for an existing one in v1.

### `POST /platform/v1/scheduled-turns/{id}/enabled`

Pause/resume: `{enabled}`. **404** if `id` is unknown (or belongs to another tenant).

### `DELETE /platform/v1/scheduled-turns/{id}`

Remove it. **204** on success; **404** if unknown.

Driven by the web **Settings → Scheduled turns** card. A background poll loop (not one
`sleep_until_hour` task per row — see [core-app](../services/core-app.md#scheduled-turns-adr-0092))
finds due rows each tick and runs them sequentially through the normal headless-turn path
(`Agent.run`), metered under the row's own tenant.

---

## Module events (ADR-0103)

The raw feed over the core's durable event log — what the *modules* announced happened. The
envelope, the emit helper, and the catalog are in [events](events.md); the delivery posture
and the log's semantics are in
[core-app](../services/core-app.md#module-event-spine--durable-intake-adr-0103).

Shell-facing, not `PlatformClient`: a module *emits* on the bus, it does not read the log
back over HTTP.

### `GET /platform/v1/events`

A snapshot, **newest first**.

Query: `tenant_id` (default: the default tenant) · `module` (exact) · `type` (exact) ·
`limit` (1–1000, default 200; **422** outside that range).

```json
[
  {
    "id": 42,
    "tenant": "local",
    "module": "mail",
    "type": "mail.received",
    "occurred_at": "2026-07-17T12:00:00Z",
    "received_at": "2026-07-17T12:00:01Z",
    "dedup_key": "gmail:18f2c1",
    "entity_ref": { "ref_id": "18f2c1", "module": "mail", "kind": "message", "title": "Re: lunch" },
    "payload": { "message_id": "18f2c1", "unread": 1 },
    "schema_version": 1
  }
]
```

`occurred_at` is the emitting module's clock (when the change happened); `received_at` is
the core's (when it heard). The `payload` is safe to render verbatim — credential-shaped
keys are rejected at emit and redacted again here.

### `GET /platform/v1/events/stream`

The same data as an SSE tail: recent history **oldest-first**, then live events. Query:
`tenant_id` · `module` · `type`. Each frame is `event: module_event` with the JSON above.

The stream never closes on its own. An event landing mid-replay may appear twice — clients
de-duplicate on `id`; the subscriber registers before the history query on purpose, since a
duplicated row is cosmetic and a missing one is not.

Driven by the Observability screen's **Events** tab
([observability](observability.md#raw-events-feed)).

---

## Automations (ADR-0105)

Core-owned Settings/page territory, shell-facing (not `PlatformClient`). The full model,
the autonomy dial, and the safety rules are in [automations](automations.md); the
Automations page itself is a companion issue (#668).

All take `tenant_id` (default: the default tenant).

### `GET /platform/v1/automations` · `POST /platform/v1/automations`

List, or create. The create body:

```json
{
  "name": "Tell me about invoices",
  "prompt": "Summarize the invoice that just arrived.",
  "autonomy": "notify",
  "event_trigger": {
    "module": "mail",
    "event_type": "mail.received",
    "matchers": [{ "field": "subject", "op": "contains", "value": "invoice" }],
    "window_start_hour": 9, "window_end_hour": 17
  },
  "model": null,
  "sinks": ["chat"],
  "chat_mode": "rolling",
  "rate_cap_per_hour": 0,
  "digest_window_minutes": 0
}
```

**400** on a blank name, an unknown autonomy level or sink, a malformed `source`, an
out-of-range hour, a negative cap, or anything other than **exactly one** trigger (pass
`schedule_trigger: {"cadence": "daily", "hour": 7}` instead for a scheduled one).

The response adds `allowed_tool_classes` — what this automation's turns may actually reach,
**derived, never stored**, so the UI shows the same allowance the tool surface enforces
rather than its own guess.

### `POST /platform/v1/automations/{id}/enabled` · `DELETE /platform/v1/automations/{id}`

Pause/resume (`{"enabled": bool}`), or remove. **404** if unknown; **204** on delete.

### `POST /platform/v1/automations/{id}/run`

Run it now — the "try it" button. An automation you cannot try is an automation you cannot
trust, and waiting until 7am to find out the prompt was wrong is not a development loop.

Goes through the **same runner** as a real trigger, so it honours the kill switch, the rate
cap, and the autonomy dial: a test run that behaved differently would be worse than none.
Recorded with a `manual` verdict. **409** when the tenant's kill switch is on; **404** if
unknown.

### `GET /platform/v1/automations/runs`

The run ledger, newest first. Query: `automation_id` · `outcome` (`ok` \| `error` \|
`skipped`; 400 on anything else) · `limit` (1–500, default 100).

```json
[
  {
    "id": "…", "automation_id": "…", "started_at": "…",
    "trigger_refs": [42], "filter_verdict": "matched",
    "model": "qwen2.5:7b", "prompt_tokens": 812, "completion_tokens": 96,
    "duration_ms": 4210, "outcome": "ok", "error": null,
    "output": "An invoice from Acme arrived.", "sinks_fired": ["chat"],
    "trigger_entity_refs": [
      { "ref_id": "…", "module": "mail", "kind": "message", "title": "Re: invoice" }
    ]
  }
]
```

Written for **every** run at every level — for `silent_act` it is the only trace.
`trigger_entity_refs` (#669) are the triggering events' `EntityRef`s, resolved from the
event log by row id so a feed renders source-entity hover-card chips; empty for
schedule/manual runs and for trigger events retention has pruned.

### `GET /platform/v1/automations/runs/stream`

The ledger as an SSE tail (#669) — the
[runs feed](observability.md#automation-runs-feed-669). Query: `automation_id` ·
`outcome`, matching `GET /runs`. Each frame is `event: automation_run` with the run-view
JSON above; recent history replays oldest-first, then live runs follow as the runner
records them — skips included.

### `GET` · `PUT /platform/v1/automations/kill-switch`

`{"halted": bool}` — stop or resume **every** automation for the tenant. Persisted, unlike
the runtime power pause: a stop a restart silently undoes is not a stop.

### `GET /platform/v1/automations/templates`

Every enabled module's preset automations — **never auto-instantiated**. Each carries
`{module, key, name, description, trigger, prompt, autonomy, sinks}`.

### `GET /platform/v1/automations/vocabulary`

`{autonomy_levels, sinks, matcher_ops}` — the closed vocabularies, so the UI never
hardcodes them.

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

The feed also carries the core's **own** queue — the agent's proposed edits to its base
instructions and playbooks (`module: "core"`, `page_id: "playbooks"`, ADR-0093). That is the
reserved **pseudo-module** the registry answers in-process rather than probing over HTTP, so it
is composed into this feed explicitly (it has no base URL to fan out to), with the same
best-effort tolerance. To every consumer it is just another entry — the point of conforming to
the existing contract rather than inventing a second one.

Because the core's queue is dispatched in-process rather than over HTTP, its best-effort
tolerance catches any exception, not just the `HTTPException` a probed module's failed request
would raise — a storage error (e.g. a degraded startup that left `playbook_proposals` uninitialized)
is logged and skipped rather than 500ing the whole feed (#657).

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

### `GET /platform/v1/calendar-feed?start=&end=`

The **cross-module calendar-feed aggregate** (#469, ADR-0088): date-anchored items — e.g. open
tasks with a due date — from every enabled, healthy module, merged and stamped with the owning
`module`. **Not a manifest-declared capability** like `resolver`/`attachable`: a module opts in
purely by serving `GET {base}/calendar-feed?start=&end=` itself; the aggregator probes every
module for that path and skips a `404`/unreachable one, the same tolerance `/suggestions` above
already relies on. `start`/`end` are floating `YYYY-MM-DD` dates, `end` exclusive (the calendar
archetype's own range convention, ADR-0023). `tasks` is the first module to implement it.

```json
[
  {
    "id": "t1",
    "title": "Renew passport",
    "date": "2026-07-15",
    "status": "open",
    "ref_id": "t1",
    "kind": "task",
    "module": "tasks"
  }
]
```

`kind` + `ref_id` + the stamped `module` are exactly what `GET /platform/v1/modules/{module}/resolve/{kind}/{ref_id}`
needs, so the shell's click handler resolves and opens a chip's hover-card generically — no
calendar-feed-specific UI contract, reusing ADR-0019 end to end.

### `GET` · `PUT /platform/v1/modules/{name}/suggestions-enabled`

The per-module **review on/off** toggle (#KB-refactor) — `{ "enabled": true }`. When **on**
(the default; a missing or NULL pref reads as `true`) the module stages agent changes on its
`review` page for approval. When **off**, the module applies them directly (it reads this via
`PlatformClient.get_suggestions_enabled` and, when off, approves its own staged suggestion
through the normal apply path). The review-page header reads `GET` and writes `PUT`; `PUT`
**404**s an unknown module. Persisted in `module_prefs`. Generic across any module with a
`review` page — today knowledge and notes.

Both verbs **403** for the reserved `core` pseudo-module — review of the agent's own
instructions/playbooks is mandatory (ADR-0093) and can never be switched off, so there is no
toggle state for `GET` to report either (#657). The shell already knows this and never queries
the endpoint for `core` (`reviewIsMandatory`).

**Except `core`**, where `PUT` is a **403**: review of the agent's own instructions and playbooks
is mandatory (ADR-0093's hard rule — agent-proposed guidance never self-applies and no path
bypasses the operator's Approve). Turning it off would advertise a bypass the reflection job
does not implement and must never implement, so the write is refused at the layer that owns the
policy, and the shell renders *Always reviewed* in place of the switch.

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
operator's **edited draft** (ADR-0090: a free-form edit, a per-hunk merge, or both layered
together), forwarded so what's written is what the operator actually approved; absent ⇒ apply
the module's full proposal unedited (for notes, the server composes the body — `append`
concatenates onto the current note). Operator-only (paired with `…/reject`, which discards).
**409** for knowledge when the target vault is externally owned (watch mode, #232); notes have
no external-owner mode. Both approve and reject record a row in the module's resolved-decision
audit trail (see the next endpoint) before the pending suggestion is dropped from the queue.

### `GET /platform/v1/modules/{name}/pages/{page_id}/audit`

The resolved-decision **audit trail** for a `review` page (ADR-0090): what a module proposed
vs. what the operator actually approved (or that it was rejected), newest first. Generic across
any module with a `review` page. Query param `limit` (default 50, 1–200). **404** if the page
isn't a `review` page or the module is unknown — same gate as approve/reject.

```json
{
  "decisions": [
    {
      "id": "9f2c…",
      "title": "goals",
      "path": "projects/goals.md",
      "operation": "update",
      "origin": "agent",
      "note": "",
      "created_at": "2026-06-24T10:00:00+00:00",
      "decided_at": "2026-06-24T10:02:00+00:00",
      "decision": "approved",
      "proposed_content": "…the agent's proposal…",
      "applied_content": "…what the operator actually approved…",
      "to_path": ""
    }
  ]
}
```

`applied_content` is empty for a `reject` (nothing was applied) or for a structural op that
carries no content (`move` / `mkdir` / `mkproject`). Each module retains up to `MAX_DECISIONS`
(200) rows per tenant, pruned oldest-first on each new decision.

### `POST /platform/v1/modules/{name}/pages/{page_id}/send`

**Mailbox-only (ADR-0087, #550).** A **human-initiated** compose/reply from the mail page. Body
`{body, to?, subject?, cc?, reply_to_message_id?}` → `{"id": <sent message id>}`. With
`reply_to_message_id` the module re-derives the recipient/subject/threading server-side (the web
never handles RFC-2822 headers); otherwise it composes from `to`/`subject`/`body`/`cc`. Gated on the
`mailbox` archetype (a non-mailbox page **404**s), and **operator-only** — it is not an MCP tool, so
the agent can never reach it (the draft-first guarantee of ADR-0085 still holds). Relays the
module's own hint on a Gmail scope/rate-limit error (**403**/**429**). It shares the module's
transmit endpoint (`POST /send`) but never the agent draft pane.

### `POST /platform/v1/modules/{name}/pages/{page_id}/mark-read`

**Mailbox-only (#625, ADR-0087).** Marks a thread's unread messages read when the reader opens it.
Body `{thread_id, message_ids}` → `{"thread_id": …, "marked": <count>}`. The core proxies to the
module's `POST /pages/{page_id}/mark-read`, which flips unread at the provider (the `set_unread`
seam, #277) and writes the read state through to the local cache (ADR-0096). Gated on the `mailbox`
archetype (a non-mailbox page **404**s), and **operator-only** — not an MCP tool, so the agent can
never mutate read-state. Relays the module's own hint on a provider error.

### `GET /platform/v1/modules/{name}/pages/{page_id}/attachment?message_id=…&attachment_id=…`

**Mailbox-only (ADR-0087).** Streams one message attachment's bytes (with its content type and a
download `Content-Disposition`) from the module through the core to the browser — nothing is stored.
Gated on the `mailbox` archetype; **404** for an unknown message/attachment.

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
