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

### Fixed

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
