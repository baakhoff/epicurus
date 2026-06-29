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
| **Chat** | Streaming agent turns (SSE readiness/delta/thinking/tool/done/error) with a warming **readiness bar** (#122) and a step-by-step **activity timeline** of the agent's thinking + tool calls that persists folded with the turn (#121, ADR-0041), session sidebar (cross-chat memory), per-chat model picker (shows each model's **size**), and last-turn **Regenerate** / inline **Edit** controls that re-answer in place (#302). **Durable across a refresh / PWA backgrounding (#376, ADR-0055):** the `sessionId` is persisted so the transcript rehydrates on reload, and an in-flight turn — which keeps running server-side regardless of the connection — is **re-attached** on a dropped stream / reload / tab-resume (`visibilitychange`/`online`) instead of showing a network error; **Stop** cancels it server-side. When the selected local model can't call tools (its `/api/show` capabilities lack `tools`), the composer shows a **"can't use tools — chat only"** hint. |
| **Memory** | What epicurus remembers across chats — the cross-chat recall corpus (ADR-0040). Browse it newest-first, **search** to see exactly what surfaces for a topic (real semantic recall), and **forget** any snippet so it stops being recalled; each links back to its source conversation. |
| **Suggestions** | **One inbox** for every module's agent-proposed changes (`GET /platform/v1/suggestions`), grouped by module — each group carries that module's review on/off toggle and its pending changes, each opening the shared review window. Replaces the per-module `review`-archetype nav entries (see **Reviewing suggested changes** below). |
| **Models** | **Catalog browser** — search and filter the model catalog by capability/tag (General, Code, Multilingual, **Vision**, **Tools**, Embedding, Small), pull with live progress. The list is **fetched from the core** (`GET /platform/v1/llm/catalog`), which parses it from upstream on a schedule (#269), with a bundled offline fallback; the screen shows its provenance. Plus the local model list: each row is a **tap-to-expand disclosure** ([per-model rows](#models--per-model-rows-328)) — collapsed it shows name + `loaded`/`default`/`hidden` badges + a **suitability status icon** (✓ fits / ⚠ tight / ✕ too big, full reason on hover; #327) + capability icons + size; expanded it reveals the model's settings inline. **Global embedding default** picker (#214) — modules with no per-module override use it, per-module selections in Modules take precedence — with a **Re-embed everything** action (#332) that rebuilds every embedding-backed module's vectors after a model change (changing the model alone doesn't re-embed existing data); a server-wide **KV-cache type** with a **hardware-aware suggested** pick (q8_0 / q4_0 on tight VRAM, f16 when ample; #329); hosted providers: status + API-key entry (stored core → OpenBao, never in the browser). |
| **Modules** | Every module's manifest-rendered config form, status, and actions. |
| **Settings** | Theme (dark/light/system), default model. |
| **Module pages** | Left-nav pages a module contributes, **core-rendered from a bounded archetype vocabulary** (ADR-0018) — the module supplies data only. |
| **Right panel** | A core-owned split-screen / bottom-sheet that opens detail views (`entity-detail`, `email-reader`, `doc-reader`) programmatically (ADR-0018). |

The **power orb** in the header (every screen) pauses/resumes and visually cools the whole
UI when paused (ADR-0005).

### Models — per-model rows (#328)

Each local model is an **inline disclosure**, not a row of hover-only icons behind a
settings *Sheet*. The old layout broke on a phone — there is no hover, so the action icons
were either invisible or pushed off-screen and the name was squeezed. Now the **whole
collapsed row is the touch target** (name, `loaded`/`default`/`hidden` badges, a suitability
status icon (#327), capability icons, size, a chevron); tapping it opens a panel that holds **every**
control: **Set as default / Unload / Hide / Delete** as full buttons, plus the per-model
**context window**, **keep-alive**, and **run-on** (GPU / CPU / Auto), and the read-only
**quantization** with a **variant pick-list** + manual *pull-variant* shortcut. One panel is
open at a time.

**Unload** (#331) drops a model from memory now (`keep_alive=0`,
`POST /platform/v1/llm/unload`) **without** changing power state — per-model in the panel when
the model is `loaded`, and **Unload all** in the card header when any is. Previously unloading
only happened as a side-effect of the power *Pause* toggle, behind a hover-only control that a
phone couldn't reach. The `loaded` badge is also kept **live**: the local-models query polls
while the page is visible and refetches on tab focus, so unloading on another device shows up
here without a reload (the old badge went stale on the PWA).

The quant pick-list (#330) is the on-demand registry lookup
(`GET /platform/v1/llm/catalog/variants`): the library catalog lists *sizes*, not quants, so
this enumerates the model's available quantizations as a tappable list — each with its quant
label, an estimated size, a **recommended** mark (the best quality that fits VRAM, from
`src/lib/quantVariants.ts`), and an `installed`/`current` badge — and pulling one reuses the
normal download flow. A manual tag box remains for non-library or HF models the lookup can't
enumerate.

The **context window is per-model and live**. The panel seeds from the model's own stored
value and reads out the tokens it will *actually* use, resolved the way the gateway resolves
it — this model's value → the **global default** → the system suggestion → 4096 — so a blank
(inherit) field still shows the inherited number and echoes it as the input placeholder.
Saving applies immediately (the models query is invalidated, **no page reload**). The
standalone **Default context window** card sets the global fallback every model inherits.

The form body (`ModelSettingsForm` in `src/screens/ModelsScreen.tsx`) is shared: it renders
inline in each row here **and** inside the embedding-default Sheet, so the two surfaces stay
identical.

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
one path, so no module ever ships its own buttons or forms. A board may also declare **view
controls** (ADR-0049) — labeled selectors (e.g. group-by, filters) the shell renders in the
toolbar; changing one re-fetches the page with a `?<id>=<value>` query param, so grouping and
filtering stay module-side while the shell stays a bounded renderer.

The `editor` archetype (knowledge, notes) opens a document **rendered** — its markdown
shows immediately, and an Edit/Preview toggle drops to the raw source when you want to
write (ADR-0042). Because notes/knowledge **re-embed on every save**, the editor does not
save on each keystroke: a save fires only when you **leave** (switch document, go back, or
the editor unmounts/backgrounds), when the doc has **idled** unchanged for a few seconds,
or when you **Save** explicitly (button / Ctrl-Cmd-S). A live status reads *Saving… →
saved* (*saved · not indexed* if the re-index round-trip failed); a **read-only** vault — a
watched Obsidian mount (ADR-0035) — never saves. The list and editor panes are each width-
and scroll-bounded (`min-w-0`, `overscroll-contain`), so on a phone the Save-bearing
toolbar never overflows the viewport and scrolling a long note never drags the bottom tab
bar.

When the page is **`versioned`** (notes, knowledge — ADR-0046), a **History** control lists
past saves; selecting one previews it read-only, and **Restore** brings it back as a fresh
save (so the timeline only ever grows). The shell reads history from the proxied
`…/doc/versions` / `…/doc/version` endpoints; restore is client-side (it re-saves a past
version's content), so there is no restore endpoint.

### Right panel / split-screen (ADR-0018)

A core-owned side panel (`src/components/Panel.tsx`, driven by the `src/stores/panel.ts`
Zustand store) opened programmatically — `open(view, payload, title)` — e.g. from a chat
entity-reference click (ADR-0019). It is a **resizable right column** on wide screens and a
**bottom sheet** on phones, with a back-stack (`back()`) and `close()`. Views are a
**bounded, core-defined vocabulary** — `entity-detail` (the hover-card envelope in full
form), `email-reader` (read-only, used by the 3.8 mail reader), and `doc-reader` (a text/`.md`
file rendered as markdown, opened from the Files browser via `GET /platform/v1/modules/storage/read`
— the split-screen reader, #KB-refactor). The panel never runs module markup.

A hover-card's optional `href` is rendered by the shared `CardLink` (`src/components/CardLink.tsx`):
an **in-app path** (`/m/…`) becomes a same-tab router navigation — e.g. a cited knowledge
note opening in the Knowledge page (#143) — an external `http(s)` URL opens in a new tab,
and any other scheme is dropped. `CardLink` is used by both the panel's `entity-detail` view
and the inline hover-card.

### Assistant prose (markdown)

Assistant replies and the editor preview render GFM markdown through `Markdown.tsx`
(`react-markdown` + `remark-gfm`, raw HTML skipped) wrapped in `.ep-prose` — the shared
typeset styles in `src/index.css`. Supported blocks: headings (`h1`–`h6`), unordered /
ordered / nested / GFM task lists, tables, block quotes, horizontal rules, links (through the
custom `a` slot, see below), and fenced code blocks with a language label + copy button
(partial fences are auto-closed mid-stream so streaming code still renders). Because Tailwind's
preflight resets heading sizes and list markers, `.ep-prose` restores them explicitly — keep
new block elements styled there or they fall back to plain paragraph text.

### Entity references in chat (ADR-0019)

An assistant message carries `entity_refs` — references to module entities. The shell
renders each as a **chip** (`src/components/EntityRef.tsx`): hover shows a core hover-card
(enriched on demand from the module's resolver via `GET /platform/v1/modules/{name}/resolve/…`),
click opens it in the right panel. A resolver may include an `href` that deep-links into a
module page — the knowledge resolver points a cited vault note at `/m/knowledge/vault?doc=…`,
and the `editor` archetype reads that `?doc=` param to open the document (#143). Refs the
assistant links inline (an `epicurus://entity/{module}/{kind}/{ref_id}` markdown link) render
inline through the Markdown `a` slot; any remaining refs collapse into a single expandable
**"Sources (N)"** pill beneath the message (`SourcesPill`, #333) that discloses the individual
chips on click — keeping a multi-source row from crowding the chat.

### Attachments in chat (ADR-0019)

The composer's **attach** affordance (`src/components/AttachMenu.tsx`) lets the user add
context to a turn: upload a **file** (`POST /platform/v1/agent/attachments`), reference
**another chat**, or pick an entity from an **enabled, attachable module** (its picker is
proxied at `GET /platform/v1/modules/{name}/attachments`). Choices appear as pills above
the input and are sent on the message as `attachments`; the agent expands them into the
turn's context. They render as pills under the user's message — beside the **optimistic
echo from the moment it is sent** (the chat store carries them on `pendingAttachments`
alongside `pendingUser`), then handed off seamlessly to the server-stored copy once the
turn lands. An
uploaded file is also kept durably in the storage module and shown in the Files page (the
upload sink, ADR-0025) — entirely server-side, so the composer is unchanged.

### Reviewing suggested changes (#KB-refactor, ADR-0033)

Every agent change to a module's content — the knowledge base **and** private **notes** — is
**staged for operator review**, never applied directly. The shell surfaces the pending queue in
two places, both reading the cross-module feed `GET /platform/v1/suggestions` (each item tagged
with its `module` + `page_id`). The feed spans **every** enabled module that declares a `review`
page, so knowledge *and* notes suggestions surface in the same bubble and inbox with no
special-casing:

- A **suggestion bubble** above the chat composer (`SuggestionBubble` in
  `src/screens/ChatScreen.tsx`) appears when the assistant has filed suggestions. It names the
  latest one ("The assistant wants to …") and shows the count when several are pending. A
  one-tap structural change (move / new folder / new knowledge base) offers **Approve** inline;
  a richer change offers **Open** (the review window). **Reject** discards the suggestion
  server-side without opening anything (#341) — for any proposal type, including folder /
  knowledge-base creation; **Ignore** only hides the bubble while the suggestion stays in the
  Suggestions inbox.
- The top-level **Suggestions** inbox (`src/screens/SuggestionsScreen.tsx`) — **one place** for
  every module's proposals. It groups the same feed by module; each group carries that module's
  **review on/off** toggle (`suggestions-enabled`) and its pending changes, each opening the
  review window. This replaces the per-module `review`-archetype nav entries (knowledge's
  *Suggestions*, notes' *Note suggestions*), which the rail now filters out — the module pages
  still exist at `/m/{module}/{review-page}` for deep links, just without their own rail link.

The **review window** (`src/components/SuggestionReviewModal.tsx`) is a core-owned overlay
shaped by the operation, with three actions — **Approve**, **Reject**, **Ignore**:

- **edit** (`update` / `create` / `append`) → a **diff with per-hunk checkboxes**: each change
  can be ticked or unticked, the accepted hunks are merged client-side (`src/lib/linediff.ts`)
  and sent as the approve `{content}` so only the chosen part is written; a `create` also offers
  a rendered preview. `append` (notes — the agent supplies only the text to add) is content-like:
  its diff shows the added text, so it reviews per-hunk like any edit.
- **delete** → a confirmation showing the document/note body that will be removed.
- **move** → a `from → to` confirmation; **new folder** / **new knowledge base** → a simple
  "create this?" confirmation.

The `ReviewSuggestion` operation enum (`src/lib/contracts.ts`) carries
`create` / `update` / `append` / `delete` / `move` / `mkdir` / `mkproject`.

Approve/reject post to `POST /platform/v1/modules/{name}/pages/{page_id}/suggestions/{id}/{action}`
(the core proxies to the module); these are operator-only — the agent never approves its own
proposals.

The **Suggestions page header** carries a per-module **review on/off** switch — *Review agent
changes before applying* (#KB-refactor, `src/components/archetypes/ReviewView.tsx`). It reads
`GET` and writes `PUT /platform/v1/modules/{name}/suggestions-enabled` (`src/lib/api.ts`:
`suggestionsEnabled` / `setSuggestionsEnabled`). When **off**, the module applies the agent's
changes directly, so the queue stays empty by design — the page shows a contextual "applied
automatically" empty state rather than "nothing awaits review". The switch is always shown
(even with an empty queue) so the operator can turn review back on.

### The chat SSE protocol

`POST /platform/v1/agent/chat/stream` returns Server-Sent Events: an optional leading
`readiness` (a warming snapshot — power state, module health, model warm; ADR-0027),
then `delta` (answer tokens), `thinking` (chain-of-thought tokens, ADR-0041), `tool` (a
tool call's `running`→`ok`/`error`), `done` (the final `AgentTurn`), `error`.

Before the first token the shell shows the turn's *process*, not a bare caret: a
**readiness bar** while the system warms (`readiness` events, #122), a **"Thinking…"** cue
once it is ready and a token is pending, then an **activity timeline** that interleaves the
model's thinking (collapsible blocks) and its tool steps **in the order they happened** —
think → call → think — each tool step with a human-readable label and live status (#121,
ADR-0041, ordering #300). The timeline folds to a one-line summary as the answer streams in.
On `done` the live turn is replaced by the clean server-stored answer — which **keeps its
folded activity**, persisted on the message (`MessageRecord.activity.timeline`), so a
reopened conversation still shows the same ordered timeline. Older turns saved before the
ordered timeline fall back to a thinking-then-steps render.

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

The shared primitive kit is one file — `src/components/ui.tsx` (`Button`, `Badge`, `Card`,
the text fields, `Switch`, `Sheet`, `Confirm`, `Tooltip`). `Tooltip` (#334) is a dependency-free
hover/focus label for **icon-only** controls — the icon keeps its `aria-label` and the wordy
label moves into the tip; used by the turn-activity summary, the board's compact "+" Add, and
the Files up-nav. Text inputs carry `min-w-0` so native date/`datetime-local` pickers can't
overflow a narrow mobile sheet (#335). `Switch` is the single on/off control used
everywhere (per-tool toggles, module enable/disable, boolean schema fields). Its **track
colour carries the state** — accent when on, muted when off — while the thumb stays a
constant, bright, evenly-inset circle that simply slides between ends. Keep that convention
so every toggle in the shell reads the same; the thumb must never change colour or sit flush
against the edge (that read as a dot escaping the pill, #245).
