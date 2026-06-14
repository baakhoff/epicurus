# web — the UI shell

**`epicurus-web`** is the web UI shell (ADR-0007) — a **phone-first PWA**: chat with the
agent, manage models and provider keys, flip the power state, and configure modules. It is
a *shell*: modules surface their UI **declaratively from their manifest**, so installing a
module makes its panel appear with **no shell rebuild and no module JavaScript in the
shell**. Host port **8084**; also the gateway's lowest-priority catch-all, so a phone on
the LAN/VPN reaches the UI at `http://<host>:8088/`.

## What it consumes

The web is a frontend — it exposes no API of its own; it consumes the core's
[platform API](../reference/platform-api.md). nginx serves the static SPA and
**same-origin-proxies** `/platform/` to the core (`CORE_APP_URL`), so there is no CORS and
SSE streams pass through unbuffered; a CSP pins the app to its own origin.

### Screens

| Screen | What it does |
| --- | --- |
| **Chat** | Streaming agent turns (SSE delta/tool/done/error), session sidebar (cross-chat memory), per-chat model picker. |
| **Models** | **Catalog browser** — search and filter a curated catalog of 24 Ollama models by tag (General, Code, Multilingual, Vision, Embedding, Small), pull with live progress; local model list (delete, hide, set global default); hosted providers: status + API-key entry (stored core → OpenBao, never in the browser). |
| **Modules** | Every module's manifest-rendered config form, status, and actions. |
| **Settings** | Theme (dark/light/system), default model. |
| **Module pages** | Left-nav pages a module contributes, **core-rendered from a bounded archetype vocabulary** (ADR-0018) — the module supplies data only. |
| **Right panel** | A core-owned split-screen / bottom-sheet that opens detail views (`entity-detail`, `email-reader`) programmatically (ADR-0018). |

The **power orb** in the header (every screen) pauses/resumes and visually cools the whole
UI when paused (ADR-0005).

### Module pages (core-rendered archetypes — ADR-0018)

A module declares `pages` in its manifest, each naming a core **archetype** —
`browser` (tree/list + detail), `calendar`, `editor`, `board`. The shell merges the pages
of reachable modules into the left nav (`modulePageNavs` in `src/app/registry.ts`) and
renders each at `/m/:module/:pageId` via a first-party screen for that archetype
(`src/screens/ModulePageScreen.tsx` → `src/components/archetypes/`). `browser` (list +
detail), `calendar` (month / week / agenda), `editor` (Obsidian-like doc), and `board`
(columns of cards) all ship today. Page data is fetched through the core proxy
(`GET /platform/v1/modules/{name}/pages/{id}`, which forwards query params such as a
calendar's `start`/`end` window) — **no module markup, JS, or CSS ever runs in the shell**.

Unlike `browser`, a `board` **mutates**: its cards and board carry declarative *actions*,
each naming one of the module's MCP tools. The shell invokes the tool through the core
(`invokeModuleTool`, validated against the manifest) — a one-tap call, a `confirm` dialog,
or a [SchemaForm](#) built from the tool's `input_schema` — then refetches the page. The
tasks module's **Tasks** page is the first board; complete/edit/add all flow through this
one path, so no module ever ships its own buttons or forms.

### Right panel / split-screen (ADR-0018)

A core-owned side panel (`src/components/Panel.tsx`, driven by the `src/stores/panel.ts`
Zustand store) opened programmatically — `open(view, payload, title)` — e.g. from a chat
entity-reference click (ADR-0019). It is a **resizable right column** on wide screens and a
**bottom sheet** on phones, with a back-stack (`back()`) and `close()`. Views are a
**bounded, core-defined vocabulary** — `entity-detail` (the hover-card envelope in full
form) and `email-reader` (read-only, used by the 3.8 mail reader). The panel never runs
module markup.

A hover-card's optional `href` is rendered by the shared `CardLink` (`src/components/CardLink.tsx`):
an **in-app path** (`/m/…`) becomes a same-tab router navigation — e.g. a cited knowledge
note opening in the Knowledge page (#143) — an external `http(s)` URL opens in a new tab,
and any other scheme is dropped. `CardLink` is used by both the panel's `entity-detail` view
and the inline hover-card.

### Entity references in chat (ADR-0019)

An assistant message carries `entity_refs` — references to module entities. The shell
renders each as a **chip** (`src/components/EntityRef.tsx`): hover shows a core hover-card
(enriched on demand from the module's resolver via `GET /platform/v1/modules/{name}/resolve/…`),
click opens it in the right panel. A resolver may include an `href` that deep-links into a
module page — the knowledge resolver points a cited vault note at `/m/knowledge/vault?doc=…`,
and the `editor` archetype reads that `?doc=` param to open the document (#143). Refs the
assistant links inline (an `epicurus://entity/{module}/{kind}/{ref_id}` markdown link) render
inline through the Markdown `a` slot; any remaining refs appear as a chip row beneath the message.

### Attachments in chat (ADR-0019)

The composer's **attach** affordance (`src/components/AttachMenu.tsx`) lets the user add
context to a turn: upload a **file** (`POST /platform/v1/agent/attachments`), reference
**another chat**, or pick an entity from an **enabled, attachable module** (its picker is
proxied at `GET /platform/v1/modules/{name}/attachments`). Choices appear as pills above
the input and are sent on the message as `attachments`; the agent expands them into the
turn's context. Persisted attachments render as pills under the user's message.

### The chat SSE protocol

`POST /platform/v1/agent/chat/stream` returns Server-Sent Events: `delta` (content
tokens), `tool` (a tool call's `running`→`ok`/`error`), `done` (the final `AgentTurn`),
`error`. The shell renders tokens live and tool calls as chips.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `CORE_APP_URL` | `http://core-app:8080` | Where nginx proxies `/platform/`. |
| `WEB_PORT` | `8084` | Host port (loopback-bound by default). |

## Data model

None — the web is stateless; conversation state lives in the core (memory). Only display
preferences (theme, default model) persist, in the browser's `localStorage`.

## Dependencies

core-app (the platform API, reverse-proxied). Everything else (fonts, icons) is vendored
into the build — zero CDN.

## Run & extend

```bash
cd services/web && npm ci && npm run dev   # dev server proxies /platform to localhost:8082
```

Vite + React + TypeScript (strict), Tailwind v4, vendored shadcn-style components, Zustand
stores, TanStack Query, zod-validated API contracts (`src/lib/contracts.ts` mirrors the
core's models). The surface registry (`src/app/registry.ts`) is **data, not markup** — new
screens add an entry, not a restructure. Installable PWA; `/platform` is excluded from the
service worker so streams always hit the network.
