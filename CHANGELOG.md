# Changelog

All notable changes to epicurus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`v0.1.0` is the first release — the first version usable on a server with a UI.

A release is cut by pushing a semver tag (`git tag v0.1.0 && git push origin
v0.1.0`); GitHub Actions then publishes the GitHub Release and versioned container
images to GHCR.

## [Unreleased]

**Phase 2 (knowledge & storage) and Phase 3 (web search + Google integrations)** —
the platform grows from the core runtime into a module fleet. Targeted for the next
bundled-stack release, **v0.2.0**.

### Added

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
