# Changelog

All notable changes to epicurus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`v0.1.0` is the first release — the first version usable on a server with a UI.

A release is cut by pushing a semver tag (`git tag v0.1.0 && git push origin
v0.1.0`); GitHub Actions then publishes the GitHub Release and versioned container
images to GHCR.

## [Unreleased]

### Added

- **Chat survives a hard refresh and PWA backgrounding** (#376, ADR-0055) — an agent turn used to
  run *inline* in the SSE request, so a dropped connection (a phone backgrounding the PWA, a hard
  refresh, a network blip) aborted it before the answer was persisted: the reply was lost and the
  client stuck on a "network error" that never ended. Turns now run **decoupled from the request**
  in a `LiveRunRegistry` — a detached task buffers the turn and always persists the answer, so a
  disconnect only drops the *listener*. The web persists its `sessionId` (the transcript rehydrates
  on reload) and **re-attaches** to a still-running turn on a dropped stream / reload / tab-resume
  (`visibilitychange`/`online`); if it finished while away, the now-durable transcript shows it.
  New: `GET /platform/v1/agent/runs/{id}/stream` (re-attach, with `after_seq`/`Last-Event-ID`),
  `GET`+`DELETE /platform/v1/agent/sessions/{id}/active-run` (rediscover / Stop), an `id:` seq on
  each chat SSE frame, and `LIVE_RUN_GRACE_SECONDS`. core-app 0.43.0→0.44.0, web 0.55.1→0.56.0.
- **One Suggestions inbox for every module's agent-proposed changes** — agent edits are staged
  for review (knowledge's vault, notes' notebook, and any module that adopts the `review`
  archetype), but each module surfaced its own queue as a separate left-nav page (knowledge's
  *Suggestions*, notes' *Note suggestions*) — two places for the same kind of thing. They are now
  a single top-level **Suggestions** surface (`src/screens/SuggestionsScreen.tsx`) that reads the
  existing cross-module feed (`GET /platform/v1/suggestions`) and **groups it by module**: each
  group carries that module's **review on/off** toggle (`suggestions-enabled`) and its pending
  changes, each opening the shared review window (Approve / Reject / Ignore). The per-module
  `review`-archetype nav entries are filtered out of the rail (`reviewPageNavs`); the pages still
  exist at `/m/{module}/{review-page}` for deep links. It shares the `["suggestions"]` query with
  the chat composer's suggestion bubble, so acting in one updates the other (`web` → 0.47.0).
- **Model capabilities are surfaced — tool support, vision, and more — and a tool-less model
  just answers in text** — the runtime reports what each model can do (`/api/show`
  `capabilities`), but nothing used it. Now: (1) the **agent offers tools only to a
  tool-capable model** — passing tools to one that can't makes the runtime error, so a
  tool-less local model falls back to a plain **text answer** and the chat composer shows a
  **"can't use tools — chat only"** hint (driven by `GET /models/details`, which gains
  `capabilities`); (2) the **Models page badges** each downloaded model with what it does
  (tools / vision / …) — `GET /platform/v1/llm/models?capabilities=true` opt-in fills them
  from `/api/show`; (3) the catalog browser gains **Tools** and surfaces **Vision** as search
  filters (the upstream `tools` capability is now mapped into the tag vocabulary); (4) the
  **chat model picker shows each model's size**. `ModelInfo`/`ModelDetails` gain `capabilities`
  (`core-app` → 0.35.0, `web` → 0.45.0).

- **Chat: the activity timeline persists and now shows the model's thinking** — the agent's
  process (its tool steps) used to disappear the instant a turn finished. Now the turn's
  **thinking + tool steps** are persisted with the message: the timeline **folds** to its
  summary rather than vanishing, and reappears folded when you reopen the conversation. The
  model's chain-of-thought is surfaced in a collapsible **Thinking** block — captured both
  from a provider's native reasoning field and from inline `<think>…</think>` spans (local
  reasoning models), and kept out of the answer. Adds a `thinking` SSE event and an additive
  `activity` JSON column on `agent_messages` (ADR-0041) (`epicurus-core` → 0.13.0,
  `core-app` → 0.23.0, `web` → 0.31.0).
- **Memory view — see and curate what epicurus remembers about you** — the cross-chat
  semantic-recall corpus (every user/assistant turn is embedded into Qdrant and the most
  similar past snippets are pulled into future chats as context) is now visible in a new
  top-level **Memory** screen. Browse it newest-first, **search** to see exactly what recall
  surfaces for a topic (the same ranking a chat turn gets), and **forget** any snippet so it
  stops being recalled — forgetting drops the recall **vector only**, leaving the source
  conversation intact. Backed by `GET /platform/v1/agent/memory?q=&limit=` and
  `DELETE /platform/v1/agent/memory/{id}`; each snippet's role + timestamp are joined from
  `agent_messages` by point id, so there's no change to the indexing path and it covers
  existing memories (closes #276, ADR-0040) (`core-app` → 0.22.0, `web` → 0.30.0).
- **The assistant knows the current time and your timezone** — the agent gained a built-in
  `now` tool (its first non-module tool) so it stops guessing the date from its training
  cutoff; combined with a new **Timezone** setting (Settings → Timezone, default `UTC`,
  editable; env `DEFAULT_TIMEZONE`) it creates calendar events at the right local date and
  time. `now` also surfaces the connected Google Calendar's timezone and flags a mismatch
  with your setting. Previously, "add it at 19:00" could land on the wrong day at the wrong
  hour. ADR-0039 (`core-app` → 0.21.0, `calendar` → 0.9.0 for the `/status` timezone,
  `web` → 0.29.0 for the Settings card).
- **Live model catalog — the core parses the model list from upstream on a schedule** — the
  Models screen's "Browse models" list used to be a hand-maintained static file
  (`services/web/src/data/catalog.ts`) that went stale and forced a web release for every new
  model. The core now owns it (constraint #8): a new `ModelCatalog` fetches a configurable
  source (`https://ollama.com/library` by default), parses each model's sizes, description,
  capabilities (→ tags) and popularity, caches the result, and refreshes it **regularly** on a
  background loop. New endpoint `GET /platform/v1/llm/catalog` → `{ entries, source, updated_at,
  stale }`; the web shell fetches it (keeping `filterCatalog` unchanged) and shows provenance
  ("From ollama.com/library · updated 3m ago"). Resilient: a failed/disabled refresh serves the
  last-good snapshot, and a small built-in **seed** when nothing has been fetched yet (cold or
  air-gapped), so the browser is never empty — the bundled list is the offline fallback. New
  knobs: `LLM_CATALOG_URL`, `LLM_CATALOG_REFRESH_SECONDS` (default 6h), `LLM_CATALOG_MAX_MODELS`
  (0 = unlimited), `LLM_CATALOG_ENABLED` (closes #269) (`core-app` → 0.20.0, `web` → 0.28.0).
- **Mail: mark messages read / unread** — mail is no longer read-only. Two new MCP tools
  (`mail_mark_read` / `mail_mark_unread`) let the agent flip a message's read state on request
  ("mark my newsletter as read"), and the right-panel email reader gains a **Mark as read /
  Mark as unread** toggle (a tool-backed action, ADR-0024) that invokes the tool through the core
  proxy and re-fetches so the toggle flips. The provider seam gains `set_unread(message_id,
  unread)`; the Gmail provider implements it via `messages.modify` on the `UNREAD` label, which
  needs the **`gmail.modify`** scope — it **replaces** `gmail.readonly` (which it supersets), so
  **an operator who connected Google before this change must reconnect once** (Settings → Connect)
  to grant it; until then the mark tools return a reconnect hint rather than a 500. No core-app
  change — the core's `/messages` and `/tools` proxies are generic pass-throughs (closes #277)
  (`mail` → 0.7.0, `web` → 0.27.0).
- **The chat composer keeps your unsent draft when you leave the page** — the message you're
  typing now lives in the chat store rather than the screen's local state, so switching to
  Models / Modules / a module page and back (which unmounts the chat screen) no longer discards
  it. The draft is restored with its auto-grown height intact and is cleared only when the
  message is actually sent. It persists for the app session (not across a full reload) (#278)
  (`web` → 0.26.0).
- **Context-window management (hardware-aware, UI-settable)** — the local runtime's context
  window (Ollama `num_ctx`) is now a persisted, per-tenant preference set from a new **Context
  window** card on the Models screen, instead of an env-var-only knob. This fixes empty replies:
  the agent's system prompt (instructions + every module's tool schemas + recalled memory) is
  sizeable, and at the default 4096-token context it filled the window with no room left to
  generate. The card probes the host — `GET /platform/v1/system/info` reports the GPU
  (multi-vendor: NVIDIA via `nvidia-smi`, AMD via `rocm-smi`/`/sys`, Intel via `/sys`, all
  best-effort and graceful) or, with no GPU, system RAM, plus the active model's on-disk size —
  and offers a **suggested range** from a documented, conservative KV-cache-per-token estimate
  (explicitly labelled an estimate, not a measured maximum). A number input + slider bound to the
  pref and a **Use suggested** button apply it; the gateway resolves the value **per turn**
  (`effective_context_window`: the pref if set, else the env default), local models only, stored
  alongside the existing defaults via the same additive `_ensure_columns` migration. The optional
  NVIDIA GPU overlay (`infra/ollama/gpu.yaml`) now also reserves the GPU for `core-app` so the
  probe can read VRAM (AMD/Intel need their own `/dev/dri` + `/dev/kfd` mounts — out of scope;
  detection degrades to system RAM without them). The chat model picker now also drives the
  warming/readiness bar for the model the turn will actually run on (not the global default), and
  the Models screen drops the confusing duplicate `chatting` badge — the persisted **default** is
  shown there, while the per-session override lives only in the chat picker (`core-app` → 0.19.0,
  `web` → 0.25.0).
- **Gemma 4 in the model browser** — the curated Ollama catalog now lists the Gemma 4 family
  (`gemma4:e2b` / `e4b` / `12b` / `26b` / `31b`), Google's multimodal (text + image) models with
  a 128K–256K context window. They show up in the Models screen and pull like any other entry
  (`web` → 0.24.0).
- **Calendar: all-day events (fixes events showing a day early) + per-create calendar picker**
  — all-day events are now modeled as a floating date range end-to-end. Google returns them
  date-only; the module coerced that to a UTC-midnight instant, which the shell then shifted
  into the viewer's local zone — landing on the **previous day** for any negative UTC offset.
  Now `Event.all_day` is carried through; all-day `start`/`end` serialize as bare `YYYY-MM-DD`
  and the shell parses them with the local `Date` constructor (no timezone shift), with an
  **"All day"** toggle in the create/edit form. The create form also gains a **picker to choose
  which calendar** a new event lands on (`calendar_create_event` accepts an optional
  `calendar_id` `account:collection` token). The local store persists `all_day` via an additive
  `_ensure_columns` migration (mirrors #248) (closes #252) (`calendar` → 0.8.0, `web` → 0.22.0).
- **Tasks: each Google list is a category, pick the list per task** — the Tasks board now
  **aggregates open tasks across every enabled list** (not just one "active" list), tagging
  each card with the list it came from, and the **Add task** form gains a **list picker** so
  you choose the category per task. Per-card Complete / Edit route back to the list the task
  belongs to; a single failing list is skipped, not fatal. Previously, enabling several Google
  lists without marking one active left the board reading the empty local store — nothing
  showed and there was no way to choose a list when adding (#253). Tasks is now `multi` like
  calendar (ADR-0036, refining ADR-0030); the web board gained a `field_choices` option type
  so a `<select>` can show a list's title while submitting its id (`tasks` → 0.8.0, `web` →
  0.23.0).
- **Connecting Google grants each module's API scopes (incremental)** — modules now declare
  the OAuth scopes they need in their manifest (`oauth_scopes`, e.g. calendar →
  `…/auth/calendar`, tasks → `…/auth/tasks`, mail → the Gmail scopes), and the web **Connect**
  button requests them: Settings connects with the **union** across all modules (one connect
  grants everything), and a module card's Connect requests just that module's scopes
  (incremental — the core accumulates). The core always includes the default identity scopes
  and unions the requested ones onto them. Previously Connect requested only `openid email
  profile`, so after connecting, the Calendar / Tasks / Gmail APIs returned 403 — the gap
  surfaced by #209 (closes #241, the #102 wiring) (`epicurus-core` → 0.12.0, `core-app` →
  0.18.0, `calendar` → 0.7.0, `tasks` → 0.7.0, `mail` → 0.6.0, `web` → 0.20.0).
- **Connecting Google auto-connects the modules that use it; settings no longer 502** —
  connecting a Google account now **auto-enables** the calendar/task-list collections of
  every module that uses it (and disconnecting clears them), so the operator connects once
  and calendar/tasks work with no per-collection toggling (builds on ADR-0030). The mail
  card's connection status is now accurate and fast — it reports whether a Google token is
  present (`is_available`) rather than making a live Gmail API call that could exceed the
  core's status-proxy timeout. And the core's module proxies (status, docs, pages, resolve,
  attachments, accounts) now map an upstream failure to a controlled response — a module's
  4xx passes through, a 5xx/timeout/connection failure becomes a clean `502` with a reason —
  instead of an unhandled exception surfacing as an opaque **Bad Gateway** when the shell
  polls a slow/erroring module. The calendar overlay also skips a single failing calendar
  rather than blanking the page (closes #209) (`core-app` → 0.17.0, `mail` → 0.5.0,
  `calendar` → 0.5.1).
- **Account/collection model: `local` is the silent default; connect Google and toggle each
  calendar/list** — calendar and tasks drop the binary `local`/`google` **provider dropdown**
  (and the `CALENDAR_PROVIDER` / `TASKS_PROVIDER` env vars). `local` is now the zero-config
  default that silently backs a module when nothing is connected, never shown as a provider.
  Connecting Google fetches **all** its collections (every calendar / task list); the operator
  toggles each on/off and picks the active one from a core-rendered **connected-accounts**
  section in the Modules screen. Calendar overlays every enabled calendar on read and writes to
  the active one; tasks is single-active. A module declares `collections` in its manifest and
  serves `GET /accounts`; the core stores the selection in `module_prefs.collections` and serves
  it (merged) at `GET·PUT /platform/v1/modules/{name}/collections` (+ a Postgres-only
  `…/collections/prefs` the module reads via `PlatformClient.get_collections`). The router falls
  back to local if the core is unreachable (local-first). ADR-0030; foundation for auto-connect
  (#209) and the editable calendar (#208) (closes #211) (`epicurus-core` → 0.11.0,
  `core-app` → 0.16.0, `calendar` → 0.5.0, `tasks` → 0.6.0, `web` → 0.18.0).
- **User-managed knowledge base: nested folders + add anything (file tree)** — the Knowledge
  editor page gains a file tree: create nested folders, add documents into any folder, and
  rename/move/delete — all path-confined to the vault (no traversal) and re-indexed on change.
  The `editor` archetype now carries an `EditorDoc.type` (`file`/`dir`) and a
  `can_manage_files` flag; the core proxies folder-create, file/folder-delete, and move CRUD
  to the module (closes #216) (`knowledge` → 0.11.0, `core-app` → 0.14.0, `web` → 0.16.0).
- **Observability page with live log console** — the web shell gains an
  `/observability` screen that streams structured logs from core-app in real time,
  without `docker logs`. The page replays up to 200 buffered history entries on
  connect, then trickles live entries as they arrive. Filters by minimum log level
  and service prefix apply server-side (no wasted bytes). Each entry shows
  timestamp, level badge, service, and message; context fields are collapsible.
  A health summary (`GET /platform/v1/readiness`) sits at the top. The stream
  reconnects automatically on disconnect (3 s back-off). Backed by a structlog
  processor injected into the chain before the renderer via the new
  `configure_logging(extra_processors=[...])` parameter (ADR-0031); secret-looking
  keys (`token`, `key`, `secret`, `password`, `credential`, `auth`) are stripped
  before any entry enters the ring buffer (#217)
  (`epicurus-core` → 0.9.0, `core-app` → 0.13.0, `web` → 0.15.0).

- **Knowledge changes are suggested for review, not pushed directly** — the agent's only
  way to change the vault is the new `knowledge_propose_edit` tool, which **stages** a
  create/update/delete instead of writing it. A new **Suggestions** page (the first `review`
  archetype) shows each pending change as a diff; the operator approves (apply + index) or
  rejects (discard) it. Direct *operator* edits (the editor save, the file-tree CRUD) stay
  immediate — the trust boundary is the author, not the action. Approve/reject are
  operator-only endpoints, never agent tools, so the agent can't approve its own proposals
  (closes #220, ADR-0033) (`epicurus-core` → 0.10.0, `core-app` → 0.15.0, `knowledge` → 0.12.0,
  `web` → 0.17.0).
- **Modules ship their own docs, auto-indexed into the knowledge base** — a module can declare
  `docs_url` in its manifest and serve `GET /docs`; the core proxies it
  (`GET /platform/v1/modules/{name}/docs`) and the **knowledge** module indexes every enabled
  module's docs on startup (and on re-index) into the shared `<tenant>__docs` collection — so
  `knowledge_search` answers questions about each service out of the box, alongside the bundled
  platform docs. Disabling a module drops its docs from retrieval. Knowledge and echo ship usage
  docs as the first examples (closes #215) (`epicurus-core` → 0.8.0, `core-app` → 0.12.0,
  `knowledge` → 0.8.0, `echo` → 0.2.1).
- **Tasks: richer fields** — tasks gain **priority, tags, and status** beyond the title/notes/
  due basics, on both the local store and (where the backend supports it) Google Tasks; the
  board view renders and edits them (#218) (`tasks` → 0.5.0, `web` → 0.14.0).
- **Global default embedding model in Settings** — the model manager gains an **embedding**
  section: pick a global default embedding model alongside the chat-model controls. Modules
  with no per-module choice use it; the per-module picker (#128) still overrides. Resolution
  order is per-module → global default → core fallback (#214) (`core-app` → 0.11.0,
  `web` → 0.13.0).
- **Per-tool enable/disable in the Modules UI** — each module card can now turn individual
  **tools** on or off, not just the whole module (#126): a disabled tool is hidden from the
  agent (it can't call it) while the module keeps running. The flag is a tenant-scoped core
  registry preference (`POST /platform/v1/modules/{name}/tools/{tool}/enabled`) and the core's
  tool exposure filters disabled tools out of the agent's tool list (#213) (`core-app` →
  0.10.0, `web` → 0.12.0).
- **Knowledge picks its embedding model (first consumer of per-module models)** — the
  knowledge module now **declares an `embedding` model slot** in its manifest, so the
  operator can choose which embedding model indexes the vault from a "Models" section on the
  knowledge card (#128, ADR-0029). The indexer resolves the choice via
  `PlatformClient.get_module_model("embedding")` and passes it to every `embed` call (vault
  indexing **and** search queries), falling back to the core default when unset. This makes
  the per-module model mechanism (shipped in #204) end-to-end exercisable; `EpicurusModule`
  gains a `required_models` argument so any module can declare slots through the builder
  (the manifest field existed but had no way to populate it). Note: embeddings are
  model-specific, so switching the model requires a **re-index** (use the card's "Re-index"
  action after changing it) (`epicurus-core` → 0.7.0, `knowledge` → 0.7.0).
- **Chat process display + readiness bar** — the chat surface now shows *what the agent is
  doing* instead of a bare streaming caret. Before the first token a **readiness bar**
  reports warming progress (module health + whether the turn's model is warm, tied to the
  power state), then a **"Thinking…"** cue, then a step-by-step **process timeline** of the
  agent's tool calls with human-readable labels (e.g. "Searching knowledge") that folds to a
  summary as the answer streams in. The core gains a readiness contract (ADR-0027): a
  queryable `GET /platform/v1/readiness` and matching `readiness` events that **lead** the
  `POST /platform/v1/agent/chat/stream` SSE turn (best-effort and time-boxed, so a slow or
  booting module never delays the answer) (#121, #122) (`core-app` → 0.9.0, `web` → 0.11.0).
- **Notes attach-to-chat — runtime-verified, `notes` → `0.2.0`** — attaching a note in
  the chat composer injects its body into that turn (a note reaches the agent **only**
  when attached; `attachable`, ADR-0019). The notes attach surface — the picker
  (`GET /attachments`) and resolve (`GET /attachments/{ref_id}` → `{title, excerpt}`) —
  shipped with the module; this promotes `notes` to its `0.2.0` milestone and adds the
  first **runtime-smoke** coverage of the chat-attachment last mile: the gate now asserts
  an attachable module's picker round-trips through the core (covering notes, knowledge,
  and calendar) (#136) (`notes` → 0.2.0).
- **Per-module model / embedding selection** — a module can declare model **slots** in its
  manifest (`required_models`: `{key, role: embedding|chat, label}`) and the operator picks
  which model fills each from a "Models" section in the module's card. The choice persists in
  `module_prefs.models` (`PUT /platform/v1/modules/{name}/models`, validated against the
  declared slots); the module fetches it with the new `PlatformClient.get_module_model(slot)`
  and passes it to `embed` / `chat`, falling back to the core default when unset. `/embed` and
  `/chat` are unchanged — per-module selection rides their existing explicit-`model` override
  (ADR-0021). First consumer: knowledge's embedding model (3.8) (ADR-0029) (closes #128)
  (`epicurus-core` → 0.5.0, `core-app` → 0.8.0, `web` → 0.10.0).
- **Module removal — confirmed container delete** — the operator can delete a module's
  **container** from the Modules screen ("Danger zone → Remove module"), behind a confirm
  dialog. The core stops + removes the container through the Docker socket via a single,
  tightly-scoped `DockerController` that touches **only a configured module's own container**
  (matched by service **and** Compose-project label) and **never** core-app, web, or a
  data-plane service. Removal **tombstones** the module (a `removed` flag on `module_prefs`)
  and is re-enforced on startup, so a `compose up` / Watchtower pull can't silently resurrect
  it. New `DELETE /platform/v1/modules/{name}` (403 protected · 503 no socket); the socket is
  mounted read-write on `core-app` only and the feature degrades to 503 without it
  (ADR-0028) (closes #127) (`core-app` → 0.7.0, `web` → 0.9.0).
- **Modules page: enable/disable + browse by tags** — the operator can turn any module
  **on or off** from the Modules screen, and search modules by name, description, or tag.
  Disabling drops the module from the agent's tools, the left-nav pages, and the chat attach
  menu while its **container keeps running** — re-enabling restores everything. The flag is a
  core-side registry preference (Postgres `module_prefs`, tenant-scoped), toggled via
  `POST /platform/v1/modules/{name}/enabled`; the module list now carries each module's
  `enabled` flag, and `ModuleManifest` gains free-text `tags`. Container *removal* stays a
  separate, privileged action (#127) (closes #126) (`epicurus-core` → 0.4.0, `core-app` →
  0.6.0, `web` → 0.8.0).
- **Tasks — agent-referenced tasks get a hover-card** — `tasks_list` now returns its open
  tasks as **entity-reference chips** (ADR-0019): hover a chip for the task's **core hover-card**
  (due date, open/completed status) and click to open it in the right-panel `entity-detail` view.
  The module declares `resolver` and serves `GET /resolve/task/{id}` over the active provider's
  `get_task`; the list tool is no longer a module-card action (an envelope can't render as a
  plain-text result, mirroring calendar / mail). The shell renders the chips, hover-card, and
  panel generically — no web change (ADR-0019) (closes #141) (`tasks` → 0.4.0).
- **Tasks — attach a task to the chat** — the tasks module becomes a **chat-attachment
  source** (`attachable`): pick an open task in the composer's attach menu and the agent uses
  it as explicit context for the turn. The module serves the picker (`GET /attachments`) and
  resolve (`GET /attachments/{ref_id}` → `{title, excerpt}`) over its open tasks; a new
  provider `get_task` backs them for both the local and Google backends. The existing core
  attach proxy and web attach menu render it unchanged — the module only supplies data
  (ADR-0019) (closes #139) (`tasks` → 0.3.0).

### Changed

- **The context-window suggestion now reflects your KV-cache type and the model's real
  limits — and is no longer clipped to 32k** — the Models-page estimate of "how big a context
  can this box hold?" assumed a fixed f16 KV cache and capped at a flat 32,768, ignoring two
  things the operator can already set/observe: the **KV-cache type** (a quantized cache
  `q8_0`/`q4_0` stores fewer bytes per token, so the same VRAM buys roughly 2×/4× the context)
  and the model's **trained context length**. The suggestion now scales the per-token KV cost
  by the active `kv_cache_type` and uses the model's trained `context_length` (read from
  `/api/show`) as the ceiling — so a long-context model on a roomy GPU can be suggested well
  past 32k, while a short-context model is never suggested beyond what it was trained for. The
  flat 32,768 survives only as the fallback when the trained length is unknown (and the lower
  CPU cap is unchanged). `GET /platform/v1/system/info` gains `kv_cache_type` and
  `model.{context_length, quantization}`; the Models page shows the model's quantization +
  trained limit and lets the token field/slider exceed 32k when supported (`core-app` →
  0.34.0, `web` → 0.44.0).
- **Long conversations are trimmed to fit the model's context window instead of overflowing
  it** — a local runtime (Ollama) silently drops whatever spills past `num_ctx`, and what
  spills first is the *oldest* context: the agent's instructions and recalled memory. With the
  default 4096 window that happens within a few turns, quietly degrading replies. The gateway
  now **compacts** every local prompt to fit before sending it (`llm/compaction.py`, applied in
  `_fit_to_context` across the blocking + streaming paths): the leading **system** messages are
  kept whole, the **most-recent** turns that fit within `num_ctx` (minus a reply reserve and the
  tool-schema footprint) are kept, older history is dropped first, a `tool` result is never
  orphaned from its `assistant` call, and the final message is always kept; a short `system`
  note marks the cut so the model knows earlier turns existed. Token counts are a conservative
  character-based estimate (no tokenizer dependency). Hosted providers (large contexts, handled
  server-side) and short chats are untouched — the latter a no-op (`core-app` → 0.33.0).
- **The observability stack (Grafana / Prometheus / Loki / Tempo / Alloy / Alertmanager) is now
  opt-in** — a self-hosted box that isn't running dashboards shouldn't pay for eight extra
  containers it never opens. Every observability service is gated behind the `observability`
  compose profile, so `docker compose up` (and `task up`) now runs a lean stack without them;
  bring them up with `docker compose --profile observability up -d` (or `task obs-up`). Nothing
  in epicurus depends on the stack at runtime — services still expose `/metrics` and `/health`,
  so an operator who prefers `docker logs` or their own monitoring can point it at those
  endpoints and never enable the profile. Infra-only; no component version change.

### Fixed

- **Uninstalling a module no longer hard-fails when the core can't reach Docker** (#382, amends
  ADR-0028) — "Remove module" returned a **503** ("the core has no Docker access") whenever the
  Docker socket wasn't mounted, leaving no way to remove a module. Removal is now **decoupled from
  the live socket**: the core writes the module's `removed` tombstone first — which hides it from
  every surface and stops routing its tools *immediately*, with or without Docker — and the
  container teardown is **deferred** to the next startup reconcile (which already re-removes any
  tombstoned module whose container is still up). The `DELETE /platform/v1/modules/{name}` response
  gains `container_teardown_deferred`; when it's true the Modules screen shows a clear
  **informational** notice ("its container is still running because the core has no Docker access;
  it will be cleared on the next restart") instead of a red error. Protected services are still
  rejected (**403**) — now before the tombstone is written, regardless of the socket — and an
  unknown module is still **404**. core-app 0.44.0→0.45.0, web 0.56.0→0.57.0.
- **A just-attached file now shows its pill immediately, not only after a reload** — when you
  attached a file and sent it, the message echoed back without the attachment pill; the pill
  only appeared once the page was reloaded (the server *had* persisted it). The optimistic
  user message carried only the text — the staged attachments were sent to the backend but
  never kept in client state — so there was nothing to render beside the bubble until the
  server transcript was refetched. The chat store now holds the staged attachments on a
  `pendingAttachments` field alongside `pendingUser` (set on send, cleared when the
  server-stored turn takes over or the session changes), and the optimistic bubble renders
  their pills exactly like the persisted message — a seamless hand-off, no reload (`web` →
  0.46.0).
- **Markdown now renders headings and lists instead of plain indented text** — assistant
  replies (and the editor preview) typeset through the shared `.ep-prose` styles, but Tailwind's
  preflight resets `h1–h6` to body size/weight and strips `list-style` from `ul`/`ol`, and the
  prose rules never restored them. So `#`/`##` headings looked like ordinary paragraphs and `-`
  / `1.` lists showed as a bare indent with no bullet or number. Restored an explicit heading
  scale + weight (h1–h6) and per-type list markers (disc / decimal / nested circle), with
  GFM task-list checkboxes, `hr`, and trimmed first/last margins. Pure styling — the markdown
  DOM was already correct (`web` → 0.43.0).
- **Scrolling over the left nav no longer scrolls the whole interface** — the fixed-height
  (`h-dvh`) app shell never clipped itself, and the side rail had no scroll region of its own.
  So once the rail's links (core surfaces + module pages + the power orb) outgrew the viewport,
  its overflow escaped to `<body>` and a wheel event anywhere over the rail dragged the entire
  UI — most visible on the Models screen. The shell now sets `overflow-hidden` (every region
  already owns its scroll) and the rail scrolls its own links; the rail also gained an
  accessible name (`aria-label="Primary"`) (`web` → 0.25.1).
- **The UI "Embedding model" choice now actually drives memory embedding** — core memory
  recall hard-coded `settings.memory_embed_model` and ignored the operator's `embed_default`
  pref, so picking an embedding model in the UI had no effect and recall 404'd if the env
  default (`nomic-embed-text`) wasn't pulled. The gateway gains `effective_embed_default`
  (symmetric with the chat `effective_default`); `embed()` with no explicit model resolves the
  pref → env default, and a module's per-module override still wins (`core-app` → 0.18.1).
- **Calendar page no longer 500s once a Google calendar is connected** — the `Event` model
  now coerces naive datetimes to UTC. The local store round-trips datetimes through a tz-naive
  DB column while Google returns tz-aware RFC3339 instants; a page overlaying both sorted a mix
  of naive and aware values and raised `TypeError: can't compare offset-naive and offset-aware
  datetimes` in `CalendarRouter.list_events`. The unit tests and CI mock the Google API (always
  aware), so only a real connected account surfaced it — caught on the live stack, not in CI
  (`calendar` → 0.7.1).
- **Tasks board (and every task read) no longer 500s on upgraded deployments** —
  `TaskStore.init()` now adds the v0.5.0 `status` / `priority` / `tags` columns to a
  pre-existing `tasks_local` table (the same `create_all` + `_ensure_columns` pattern as
  `llm_prefs` / `module_prefs` / the memory store). A database provisioned before #218 lacked
  those columns, so the board page, the `tasks_list` tool, the attachment picker, and the
  resolver all 500'd with `column tasks_local.status does not exist`. Fresh installs were
  unaffected, so CI and the unit tests (SQLite, always built fresh) didn't catch it (#247)
  (`tasks` → 0.7.1).
- **Module docs are actually indexed (moved off the Swagger-reserved `/docs`)** — modules now
  serve their contributed docs at **`/module-docs`**, not `/docs`. `/docs` is FastAPI's built-in
  Swagger UI, which shadowed the route, so the core's docs proxy fetched HTML and the knowledge
  indexer recorded **0** module docs (#215 was effectively a no-op at runtime). echo and
  knowledge now declare `docs_url="/module-docs"` and serve it there; the manifest field doc
  warns against `/docs`. Also realigns echo's manifest version, which had drifted behind its
  package version (`echo` → 0.2.2, `knowledge` → 0.8.1).
- **Existing deployments: `llm_prefs` gains its new columns in place** — `LlmPrefsStore.init()`
  now adds the `global_default` / `embed_default` columns to a pre-existing table (the same
  `create_all` + `_ensure_columns` pattern as `module_prefs` / the memory store). Without it, a
  database created before the global-embedding default (#214) 500s on every prefs and embedding
  read (`column llm_prefs.embed_default does not exist`), which also broke module-docs indexing
  (knowledge embeds → resolves the embedding default → 500). Fresh installs were unaffected, so
  CI didn't catch it (`core-app` → 0.12.1).
- **Modules page: clearer enable/disable toggle** — the module on/off control no longer
  renders as an ambiguous half-set slider; enabled vs disabled is now visually unmistakable
  (#212) (`web` → 0.11.1).

### Security

- **Bounded chat uploads + module-proxy path segments** (#175) — the attachment upload
  route (`POST /platform/v1/agent/attachments`) now enforces a size cap (**413** above
  `ATTACHMENT_MAX_BYTES`, 10 MiB default) and a content-type allowlist (**415**,
  `ATTACHMENT_ALLOWED_TYPES`), and the web container's nginx caps `/platform/` request
  bodies at the edge (`client_max_body_size 12m`) — previously the core endpoint was
  unbounded on the internal network and silently limited to nginx's 1 MB default. The
  module registry also rejects `/`, `\`, or `..` in the `ref_id` / entity `kind` /
  `page_id` segments it interpolates into a module request (**400**, defense-in-depth).
  (`core-app` → 0.5.1.)

### Dependencies

- **fastapi 0.137.1, mcp 1.28.0, litellm 1.89.1** (supersedes #203) — FastAPI 0.137 makes
  `include_router` attach a lazy `_IncludedRouter` to `app.routes` instead of eagerly
  flattening the included sub-routes, so the long-standing `[r.path for r in app.routes]`
  idiom stopped seeing nested routes (`/health` and friends vanished from the list, which
  failed every service's app-route test). The endpoints themselves were never affected —
  only introspection. New shared helper **`epicurus_core.route_paths(app)`** flattens the
  route tree across this change (and older FastAPI), and the service app-route tests use it.
  Also realigns the drifted `epicurus_core.__version__` (was `0.3.0`) with the package
  version (`epicurus-core` → 0.6.0).

## [0.2.0] — 2026-06-14

**Phase 2 (knowledge & storage) and Phase 3 (web search + Google integrations),
consolidated through Phases 3.5 / 3.7 / 3.8.** The platform grows from the core runtime
into a module fleet with a module-contributed UI — the first public release.

### Added

- **Notes module + page (attach-only, RAG-indexed)** — a new **`notes`** module: a
  **Notes** left-nav page (the `editor` archetype) to write notes in the ε editor, each
  saved to Postgres (the source of truth) and indexed into its **own** tenant-scoped Qdrant
  collection. Notes are **attach-only** — the module exposes **no agent tool**, so the
  assistant reads a note only when the user **attaches** it to a message (`attachable`,
  ADR-0019); this is the line between Notes (you author + manually attach) and Knowledge
  (your vault, agent-retrievable). The shared core editor gains in-app **authoring** — a
  "New note" control creates documents through the existing save path, opt-in per page via
  `EditorData.can_create` (knowledge keeps authoring in Obsidian) (ADR-0018 / ADR-0022 /
  ADR-0026) (new `notes` → 0.1.0, `web` → 0.7.0).
- **Cited knowledge documents get a hover-card** — when the agent cites a vault note or a
  platform-docs page (a `knowledge_search` result), it now renders in chat as an
  **entity-reference chip**: `knowledge_search` returns a `ToolEnvelope` and the module
  serves the resolver (`GET /resolve/knowledge/{ref_id}`). Hovering shows the core hover-card
  (path, tags, last-indexed); clicking a vault note **opens it in the Knowledge page** via a
  deep link the `editor` archetype reads (`?doc=`). The web learns to render an **in-app**
  hover-card link as a same-tab router navigation (the shared `CardLink`, used by the panel
  and the inline card). `knowledge_search`'s long-documented `docs/` prefix for platform-docs
  citations is now actually applied (ADR-0019) (`knowledge` → 0.6.0, `web` → 0.7.0).
- **Attach a knowledge document to the chat** — the knowledge module becomes a
  **chat-attachment source** (`attachable`): pick a vault document in the composer's attach
  menu and the agent uses it as explicit context for the turn, beyond default retrieval. The
  module serves the picker (`GET /attachments`) and resolve (`GET /attachments/{ref_id}`)
  over its vault; a document is named by an **opaque base64url `source:path` ref** so its
  path round-trips as a single URL segment. The existing core attach proxy and web attach
  menu render it unchanged — the module only supplies data (ADR-0019) (`knowledge` → 0.5.0).
- **Calendar — events as chat chips, hover-cards & attachments** — `calendar_list_events` now
  returns its events as **entity-reference chips** (ADR-0019): hover a chip for the event's **core
  hover-card** (when / location / calendar) and click to open it in the right-panel
  `entity-detail` view. The module declares `resolver` and serves `GET /resolve/event/{id}`, and
  becomes a **chat-attachment source** (`attachable`) — the composer can attach an upcoming event
  (`GET /attachments` picker + `GET /attachments/{id}` resolve → `{title, excerpt}`) so the agent
  uses its details. A new provider `get_event` backs all three surfaces for both the local and
  Google backends; the list tool is no longer a module-card action (an envelope can't render as a
  plain-text result, mirroring mail) (closes #138, #140) (`calendar` → 0.4.0).
- **Chat uploads land in storage (the upload sink)** — a file attached in chat is now
  durably persisted to the **storage** module's object store and becomes browsable under an
  **`uploads/`** folder in the Files page (downloadable like any file), in addition to the
  core-side handle the agent reads. Storage gains a binary object surface
  (`put_bytes`/`get_object`) and `POST /ingest`, which catalogues each upload with a new
  `source` marker so a filesystem rescan never purges it; `/download` streams object uploads
  from MinIO. The core's attachment-upload route best-effort forwards the bytes to the new
  `attachment_sink_url` — a failed or absent sink never breaks the upload (ADR-0025)
  (`storage` → 0.3.0, `core-app` → 0.5.0).
- **Knowledge page (browse + edit, Obsidian-style)** — the knowledge module contributes an
  **`editor`** left-nav page: browse the vault's documents and read/edit them in a
  core-rendered markdown editor (source **and** preview), saving back to the vault. A save
  **re-indexes just that document**, so edits made in the shell are immediately
  agent-retrievable. This introduces the **shared core doc-editor component** (a future
  Notes module reuses it) and the editor doc read/write proxy
  (`GET|PUT /platform/v1/modules/{name}/pages/{id}/doc`, editor-only); the knowledge vault
  mount becomes **read-write** and document paths are strictly confined to the vault (no
  traversal). The `knowledge` package version is also realigned with its manifest (the
  pyproject had drifted behind the shipped 0.2/0.3 features) (ADR-0018) (`knowledge` →
  0.4.0, `core-app` → 0.4.0, `web` → 0.6.0).
- **Module-contributed pages** — modules can add **left-nav pages, core-rendered from a
  bounded archetype vocabulary** (`browser` / `calendar` / `editor` / `board`): a module
  declares a `PageSpec` and serves its data, the shell renders it — **no module markup, JS,
  or CSS**, and modules can't invent a view type. The `browser` archetype (list + detail)
  ships first; echo gains a demo **Echoes** page. Page data is proxied through the core
  (`GET /platform/v1/modules/{name}/pages/{id}`) (ADR-0018) (`epicurus-core` → 0.3.0,
  `core-app` → 0.3.0, `web` → 0.5.0, `echo` → 0.2.0).
- **Calendar page** — the calendar module contributes a **Calendar** left-nav page in the
  `calendar` archetype (ADR-0018): month / week / agenda views the **core renders** from the
  module's "events in a range" data. Navigation re-fetches the visible window — the core page
  proxy now **forwards query params** (`start`/`end`) to the module — so the calendar scrolls
  arbitrarily far without loading every event. Read-first (view + navigate); the active
  provider (local or Google) supplies the events (`calendar` → 0.2.0, `core-app` → 0.3.1,
  `web` → 0.6.0).
- **Tasks page — the first `board`** — the tasks module gains a **Tasks** left-nav page: a
  core-rendered `board` of open tasks grouped by due date (Overdue / Today / Upcoming / No
  date) where the user **completes, edits, and adds** tasks. The `board` archetype is new in
  the shell; a board's cards and toolbar carry declarative **actions** that invoke the
  module's MCP tools through the core (one-tap, a confirm dialog, or a SchemaForm prefilled
  from the tool's `input_schema`), so a core-rendered view mutates with **no module markup**.
  Editing is backed by a new `tasks_update` tool (ADR-0018) (`tasks` → 0.2.0, `web` → 0.6.0).
- **Right-panel / split-screen host** — a core-owned side panel: a resizable right column
  on wide screens, a bottom sheet on phones, opened programmatically with a back-stack. It
  renders a **bounded, core-defined** set of views (`entity-detail`, `email-reader`) — the
  substrate the chat entity-reference click and the 3.8 mail reader build on (ADR-0018)
  (`web` → 0.5.0).
- **Chat entity references** — the assistant can mention a module entity (event / task /
  email / doc) as an **interactive chip**: hover → a core hover-card, click → opens in the
  right panel. A tool emits refs by returning a `ToolEnvelope`; the agent lifts them onto the
  turn and persists them on the message (a chat-schema migration adds `entity_refs`). The
  hover-card is resolved on demand from the module's declared `GET /resolve/{kind}/{ref_id}`,
  proxied by the core; echo ships a reference resolver (ADR-0019) (`epicurus-core` → 0.3.0,
  `core-app` → 0.3.0, `web` → 0.5.0, `echo` → 0.2.0).
- **Mail hover-cards show unread status** — an agent-referenced email's hover-card now
  reports whether the message is **unread**: the resolver leads its detail rows with a
  `Status: Unread` row (read messages omit it). The provider-agnostic `MailMessage` gains an
  `unread` flag the Gmail provider derives from the `UNREAD` label. The resolver, the
  `email-reader` panel, and the chip-click target shipped earlier with the mail reader; this
  completes mail's entity-reference surface. Clicking still opens the read-only reader, so the
  hover-card carries no `href` (in-app panel navigation, not an outbound URL). The shell needs
  no change — it renders hover-card detail rows generically (ADR-0019) (`mail` → 0.4.0).
- **Chat attachments** — the user can attach context to a turn: an uploaded **file** (held
  core-side via `POST /platform/v1/agent/attachments`), another **chat**, or an entity from
  an **enabled, attachable module**. The composer gains an attach affordance with pills; the
  agent expands each attachment into the turn's context. A chat-schema migration adds
  `attachments`; a module opts in as a source with `attachable` + a picker / resolve
  (ADR-0019) (`epicurus-core` → 0.3.0, `core-app` → 0.3.0, `web` → 0.5.0).
- **Model catalog browser** — replaces "type a name to pull" with a browsable catalog of 24
  curated Ollama models. Search by name, family, or description; filter by tag (General, Code,
  Multilingual, Vision, Embedding, Small); pull any entry with live SSE progress. The
  `src/data/catalog.ts` module is the seam: swap it for a `GET /platform/v1/llm/catalog`
  fetch when live Ollama-registry browse lands (`web` → 0.4.0).
- **Code-block copy button** — a one-click copy button with a language label appears on
  every fenced code block in assistant messages. Streaming partial fences are
  pre-closed so they render as code rather than raw text mid-stream (`web` → 0.3.0).
- **Knowledge module** — Obsidian-vault RAG: incremental ingestion into Qdrant and a
  `knowledge_search` retrieval tool for the agent. epicurus also indexes its own
  `docs/` tree by default, so the assistant can answer questions about the platform
  (ADR-0013).
- **Storage module** — indexes the on-disk file tree with browse / search / download
  APIs and agent file tools, plus a **MinIO** object store for app-managed objects.
- **Web search** — self-hosted **SearXNG** with a `web_search` MCP tool.
- **Connected accounts (OAuth 2.0)** — core-managed Authorization-Code flow with a
  per-tenant token vault and transparent refresh, plus a "Connected accounts"
  Settings screen to connect / disconnect providers and grant scopes incrementally.
  Modules fetch tokens through the platform API and never hold client secrets
  (ADR-0020).
- **Calendar module** — provider-neutral calendar with **local** and **Google**
  providers behind one tool surface (ADR-0016).
- **Mail module** — Gmail provider v0.1: `mail_search`, `mail_read`, `mail_send`.
- **Tasks module** — provider-neutral tasks (`tasks_list`, `tasks_add`,
  `tasks_complete`) with **local** and **Google** providers (ADR-0016).
- **Platform inference API** — `embed` + `chat` over the core LLM gateway, exposed to
  modules through `PlatformClient`; modules never call models directly.
- **Shared chat contract** — `ChatMessage` and `ChatResult` are exported from
  `epicurus_core` as the single source of truth for the chat shapes the gateway,
  platform API, and `PlatformClient` all use; `PlatformMessage` / `PlatformChatResponse`
  remain backward-compatible aliases (ADR-0021).
- **LLM tuning via env** — `LLM_TEMPERATURE`, `LLM_TOP_P`, and `LLM_NUM_CTX` (alongside
  the existing `LLM_KEEP_ALIVE`) flow compose → settings → gateway, so tuning needs no
  code edit (ADR-0021).
- **Versioning policy** — per-component SemVer plus a bundled-stack release tag;
  every PR and dispatch brief declares its version bump (ADR-0017).
- **Runtime smoke gate** — CI boots the whole stack on every PR and asserts the
  integration last mile (image tags, mounts, module discovery, one MCP round-trip),
  catching breakage that lint and `compose config` miss (ADR-0015).
- **Always-on deployment** — start-on-boot runbook for Windows (Docker Desktop
  launch-on-login), Prometheus alert rules (service down, OpenBao sealed, disk > 85%),
  Alertmanager for notification routing, and a minimal backup posture: volume snapshot
  script (`infra/backups/backup.sh`) with a verified restore procedure (#115).

### Changed

- **Pinned image tags** — all service compose fragments now use
  `${EPICURUS_VERSION:-latest}` instead of hard-coded `:latest`. Local dev
  continues to work without any change; staging / prod deployments set
  `EPICURUS_VERSION=<semver>` in `.env` to pin every service to a known-good,
  immutable image (see `docs/developer/releases.md` and `.env.example`).
- **One module-facing chat path** — `POST /platform/v1/chat` is the single module → core
  chat endpoint and returns the shared `ChatResult`; the gateway's duplicate
  `POST /platform/v1/llm/chat` was removed (ADR-0021).
- **Component versions** — `core-app`, `epicurus-core`, and `web` move to **0.2.0** to
  reflect the user-visible capability shipped since v0.1.0 (ADR-0017); the six modules
  added this cycle are at their first `0.1.0`.
- **Persistent secrets** — OpenBao moves from dev (in-memory) mode to file storage
  with an init / unseal lifecycle, so provider keys and module config survive a
  restart (ADR-0014). Resolves the v0.1.0 "secrets are not yet persistent" limitation.
- **Documentation** — a navigable `docs/` tree with a page per service / module and a
  full reference section (ADR-0013).

### Removed

- **`POST /platform/v1/llm/chat`** — folded into `POST /platform/v1/chat`, a strict
  superset (it also accepts `tools` and `tenant_id`). `PlatformClient` already used
  `/chat`, so live module code is unaffected (ADR-0021).

### Fixed

- Stability fixes across the data plane and modules: the MinIO client image tag,
  knowledge `mtime_ns` stored as `BigInteger`, the OpenBao bootstrap
  (init / unseal / policy / token), the SearXNG image tag and settings mount, and the
  pytest `importlib` import mode.
- **Smoke gate isolation** — `infra/ci/compose.ci.yaml` resets host ports for the
  wave-2 modules (calendar, mail, tasks) too, so `task smoke` runs alongside a
  developer's dev stack without port collisions (#114).

### Dependencies

- Routine dependency refresh (Dependabot): CI Actions repinned to current SHAs
  (`checkout` → v6, `setup-uv` → v8, `setup-node` → v6, `gitleaks-action` → v3,
  `docker/login-action` → v4); Python deps (`uvicorn` ≥0.49, `sqlalchemy` ≥2.0.50,
  `testcontainers` ≥4.14.2); web deps (`jsdom` → 29, `lucide-react` → 1.x, plus a
  dev-dependency group). The `eslint` 10, `@vitejs/plugin-react` 6, and one
  Python-group bump are **deferred pending migration** (tracked in #172).
- Declared the `sqlalchemy[asyncio]` ≥2.0.50 floor in the five service
  `pyproject.toml` manifests (calendar, core-app, knowledge, storage, tasks). The
  Dependabot bump (#168) had raised it in `uv.lock` only, leaving the source
  manifests at ≥2.0 — `uv.lock` and the manifests now agree. No resolution change
  (sqlalchemy stays 2.0.50).

## [0.1.0] — 2026-06-12

**Phase 1 — the core runtime.** The platform runs end to end: chat from a phone with
a local or hosted model that calls tools and remembers across sessions.

### Added

- **Agent** — a thin MCP tool-calling loop with streaming chat (SSE).
- **LLM gateway** — one provider-agnostic interface over local **Ollama** and hosted
  providers (Claude, ChatGPT, Grok, DeepSeek, Gemini, and any OpenAI-compatible
  endpoint): routing, fallback chains, and tenant-scoped usage accounting. Keys live
  in OpenBao, never in env or logs.
- **Power states** (Active / Idle / Paused) with idle model unload (ADR-0005).
- **Cross-chat memory** — conversation history in Postgres plus semantic recall over
  Qdrant embeddings, scoped per tenant.
- **Web UI shell** — a phone-first PWA (chat, model manager, provider keys, power
  toggle) that renders each module's UI declaratively from its manifest (ADR-0007).
- **Module manifest UI** — `UiSection` / `UiAction`, served at `GET /manifest`.

### Known limitations

An early `0.x` release for personal / self-host use:

- **Secrets are not yet persistent** — OpenBao runs in dev (in-memory) mode, so
  provider keys and module config are lost when the `openbao` container restarts.
  Persistent secret storage lands in Phase 3.
- **The event bus has no authentication** — NATS tenant isolation is cooperative
  (fine single-user, not multi-tenant). Tracked in #50.
- **No perimeter is bundled** — the edge gateway only routes; put your own access
  layer (VPN / reverse proxy / auth proxy) in front (ADR-0008).
