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
| **Chat** | Streaming agent turns (SSE readiness/delta/thinking/tool/done/error) with a warming **readiness bar** (#122) and a step-by-step **activity timeline** of the agent's thinking + tool calls that persists folded with the turn (#121, ADR-0041), session sidebar (cross-chat memory), per-chat model picker (shows each model's **size**), and last-turn **Regenerate** / inline **Edit** controls that re-answer in place (#302). **Durable across a refresh / PWA backgrounding (#376, ADR-0055):** the `sessionId` is persisted so the transcript rehydrates on reload, and an in-flight turn — which keeps running server-side regardless of the connection — is **re-attached** on a dropped stream / reload / tab-resume (`visibilitychange`/`online`) instead of showing a network error; **Stop** cancels it server-side. The re-attach retry (#477) distinguishes an opportunistic **probe** (mount/`visibilitychange`/`online`, with no evidence a turn is even running) from a confirmed **recovery** (a 409 on send, or a stream that dropped mid-turn, or a probe that *did* find a live run before losing it again): only a recovery that exhausts its retry budget shows the "lost connection" banner, with an in-place **Reconnect** action (re-runs the same probe — the transcript endpoint already has the answer, no reload needed); a pure probe that never confirms anything real just gives up quietly, and the next mount/`visibilitychange`/`online` gets a fresh retry budget for free. When the selected local model can't call tools (its `/api/show` capabilities lack `tools`), the composer shows a **"can't use tools — chat only"** hint. When the assistant calls **`ask_user`** to clarify (#360, ADR-0053), the turn pauses and an **inline question + answer input** appears in the live turn; answering resumes it (`POST …/runs/{id}/resume`) and the persisted prompt survives a refresh. The **Conversations list** marks each chat that has an in-flight turn with a subtle **pulsing accent dot** (#396) — the current chat from its own live state, other sessions polled from `GET /agent/active-runs` while the list is open. The **header names the open conversation** (serif title, or an italic *New conversation* placeholder), so switching sessions always shows where you are (#480). The Conversations sheet **groups sessions by recency** (Today / Yesterday / This week / This month / Earlier — `recencyBucket` in `src/lib/format.ts`), offers a **title search** (matches flat-listed while searching), and **never deletes without confirming**; deleting the *open* conversation starts a fresh one rather than leaving an orphaned transcript on screen. Scrolling up to re-read — including during a stream — surfaces a sticky **jump-to-latest** button that re-pins the view. Every assistant turn offers **Copy** (always visible on the latest turn, hover/focus-revealed on earlier ones); copying goes through `src/lib/clipboard.ts`, which falls back to the legacy selection path on plain-HTTP LAN origins where `navigator.clipboard` doesn't exist. A fresh conversation shows **module-aware starter chips** beneath a day-rotating Epicurus quote — a shell-owned mapping keyed by installed (enabled + healthy) module names; a chip fills the composer and focuses it, never sends (#480). |
| **Memory** | What epicurus remembers across chats — the cross-chat recall corpus (ADR-0040). Browse it newest-first, **search** to see exactly what surfaces for a topic (real semantic recall), and **forget** any snippet so it stops being recalled; each links back to its source conversation. |
| **Suggestions** | **One inbox** for every module's agent-proposed changes (`GET /platform/v1/suggestions`), grouped by module — each group carries that module's review on/off toggle and its pending changes, each opening the shared review window. Replaces the per-module `review`-archetype nav entries (see **Reviewing suggested changes** below). |
| **Models** | **Catalog browser** — search and filter the model catalog by **multi-select** tags (General, Code, Multilingual, **Vision**, **Tools**, Embedding, Small — combined with **AND**, so a model must carry every checked tag; "All" clears them; #389), plus, once the system is known, a **fit-rating filter** (Fits / Tight / Too big — each model's estimated size judged against your hardware; #388); pull with live progress (a freshly pulled model is given a **recommended per-model context window** sized to itself, not the global default; #386). The list is **fetched from the core** (`GET /platform/v1/llm/catalog`), which parses it from upstream on a schedule (#269), with a bundled offline fallback; the screen shows its provenance. Plus the local model list: each row is a **tap-to-expand disclosure** ([per-model rows](#models--per-model-rows-328)) — collapsed it shows name + `loaded`/`default`/`hidden` badges + a **suitability status icon** (✓ fits / ⚠ tight / ✕ too big, full reason on hover; #327) + **icon-only capability badges** (tools/vision/…, label on hover; #384) + size; expanded it reveals the model's settings inline. **Global embedding default** picker (#214) — modules with no per-module override use it, per-module selections in Modules take precedence — with a **Re-embed everything** action (#332) that rebuilds every embedding-backed module's vectors after a model change (changing the model alone doesn't re-embed existing data); a server-wide **KV-cache type** with a **hardware-aware suggested** pick (q8_0 / q4_0 on tight VRAM, f16 when ample; #329); hosted providers: status + API-key entry (stored core → OpenBao, never in the browser). |
| **Modules** | Every module's manifest-rendered config form, status, and actions. |
| **Settings** | Theme (dark/light/system), **connected accounts** (OAuth client credentials + connect/disconnect), **chat bridges** (connect/disconnect external messaging channels like Discord — a write-only bot token, an on/off switch, and live per-bridge status; #369, ADR-0062 — the card itself only renders once the **messaging module is installed and enabled**, #430), **timezone**, **agent cycles**, platform info, and memory. The connected-account and bridge rows keep their credential/disconnect actions **icon-only** (label via the shared `Tooltip` + `aria-label`) so they never overflow a phone (#393); every field uses the one themed field style (#394). |
| **Module pages** | Left-nav pages a module contributes, **core-rendered from a bounded archetype vocabulary** (ADR-0018) — the module supplies data only. |
| **Right panel** | A core-owned split-screen / bottom-sheet that opens detail views (`entity-detail`, `email-reader`, `doc-reader`) programmatically (ADR-0018). |

The **power orb** in the header (every screen) pauses/resumes and visually cools the whole
UI when paused (ADR-0005).

### App shell & viewport (mobile chrome)

The shell (`src/App.tsx`) is a **fixed-viewport** layout: `#root` is taken out of flow
(`position: fixed`, `overflow: hidden`) so the document body never scrolls — every region
owns its own scroll, and a wheel over the static side rail can't drag the whole interface
(#273). On **wide screens** a left **side rail** carries the nav and the power orb; on a
**phone** that collapses to a **top bar** (wordmark + power orb) and a **bottom tab bar**
(the primary nav). With the module pages aboard, the tab bar overflows a phone viewport —
`MobileTabBar` (`src/App.tsx`) marks the hidden side(s) with canvas-coloured gradient
**edge fades** (left/right, only while content remains that way), so the horizontal scroll
is discoverable instead of silently cutting off Calendar/Tasks/Settings (#480). The main
column stacks header · routed screen · bottom tab bar, alongside
the right panel and the shared corner notification stack (`CornerStack`, #510 — below).

`#root` is sized to the **dynamic viewport** (`height: 100dvh`, anchored at `top: 0`) and the
shell fills it with `h-full` — one viewport measurement, shared. This is deliberate: pinning
the fixed root to the *large* viewport (`inset: 0`) while the shell independently measured the
*dynamic* viewport (`h-dvh`) let the two disagree on a phone while the address bar is showing —
i.e. right after a **refresh** — so the bottom tab bar, anchored to the bottom of the
`overflow-hidden` shell, was clipped out of view until you scrolled and the bar retracted.
On **Android PWA**, `dvh` itself can still misreport for a moment right after a reload (#429):
`useViewportMirror` (`src/lib/viewport.ts`) mirrors the live `visualViewport` height into a
`--app-height` custom property on mount/resize, and `#root` prefers that over the raw `dvh`
value once it's set. Notch / home-indicator insets are handled with `pt-safe` / `pb-safe`
(`env(safe-area-inset-*)`).

**On-screen keyboard (#476).** The viewport meta declares
`interactive-widget=resizes-content` (Chromium 108+): opening the keyboard resizes the
*layout* viewport itself, so `dvh`/`--app-height` shrink to match and the fixed shell (bottom
nav included) never moves. Without it, Android's default `resizes-visual` instead *pans* the
visual viewport within an unchanged layout viewport — dragging everything anchored to that
layout viewport, tab bar included, along with the gesture. iOS Safari and pre-108 engines
ignore the directive harmlessly (today's behavior, unchanged there); `useViewportMirror` also
mirrors `visualViewport.offsetTop` into `--app-offset-top`, which `#root`'s `translateY`
cancels out as a fallback on any engine where the pan still happens — zero-cost (offset stays
0) wherever the primary fix already prevents it. `html`/`body` additionally declare an
explicit `overflow: hidden` + `overscroll-behavior: none` as a defensive statement of the
"the document itself is never a scrollable" invariant `#root`'s fixed positioning already
enforces in practice.

### Design tokens & contrast (#490)

The `--ep-*` tokens (`src/index.css`) carry a **WCAG AA guarantee**: every text-role token
(`text`, `text-dim`, `text-faint`, `ok`, `warn`, `danger`) holds **≥ 4.5:1 against all three
backgrounds** (`canvas`, `surface`, `surface-2`) in **both themes**, and each accent family's
`-strong` member clears the badge worst case (its own translucent `-dim` fill composited over
`surface-2`). **Accent-as-fill** carries its own pair (#505): `on-accent` is the label on an
accent fill (Button primary, ActionControl's primary segment, the calendar *today* pip) and
`accent-hover` the hovered fill beneath that same label — `accent-strong` is **not** a fill
token (in the light theme it is text-grade badge ink, far too dark to sit under the label).
Both accent families (gold awake / moon paused) hold ≥ 4.5:1 at rest *and* under hover in
both themes; the light theme forces a near-black label on gold but a **white** one on the
paused moon fill — a mid-tone that neither near-black (4.0:1) nor the paper canvas (4.2:1)
can clear. `src/test/contrast.test.ts` parses the CSS and enforces exactly this, plus the
`faint < dim < text` quietness hierarchy — change a token and the suite tells you whether it
still complies. Two consequences worth knowing: the light theme's muted pair is deliberately
compressed (paper backgrounds leave little luminance room under the AA floor), and the light
theme re-tunes the semantic trio rather than inheriting the dark hexes. Decorative/disabled
uses (dots, watermarks) are exempt by convention, but since the tokens themselves comply, no
call site has to reason about it. Phone tab labels render at 11px minimum — primary
navigation never sits at the app's smallest text size.

**Runtime colours get the same discipline, computed (#531).** A calendar's colour arrives
from provider data (hex) or is derived per id (`hsl(h 55% 58%)`) — no static token can pair
text with it. `src/lib/color.ts` computes the on-colour per fill: the first of **house ink →
white → pure black** to clear 4.5:1 (the black/white pair mathematically guarantees a
compliant pick for any fill). The calendar event chip's hover uses it via a `--cal-ink`
inline variable next to `--cal`; `src/test/color.test.ts` holds the AA floor across every
derivable hue and the classic Google palette. Any future surface that fills with a
runtime colour should set its text with `onColor(fill)`, never with a theme token.

### Overlay focus management (#487)

`Sheet` and `Confirm` (`src/components/ui.tsx`) honor the full modal keyboard contract via
a shared `useModalFocus` hook (hand-rolled, dependency-free): on open, focus moves into the
dialog — the container itself by default, `Confirm`'s **Cancel** button as the safe default
under a destructive prompt, and **neither** if a child rendered with `autoFocus` (search /
rename fields) already claimed it. While open, Tab/Shift+Tab wrap inside the dialog; on
close, focus returns to the element that triggered it. `Confirm` also cancels on Escape —
registered in the **capture phase** with `stopPropagation`, so a Confirm stacked above an
open Sheet (e.g. delete-session over the sessions sheet) closes alone instead of taking the
sheet with it.

`useModalFocus` is **exported** for the hand-rolled `role="dialog"` overlays that live
outside the kit: the calendar archetype's **EventDetail** adopted it (#512) and the
**command palette** (#491) is the input-first case, so Sheet, Confirm, the event detail,
and the palette all honor the one keyboard contract. Any new dialog must either build on
`Sheet`/`Confirm` or wire this hook — `SuggestionReviewModal` is the known remaining
hand-rolled exception. One ordering subtlety the palette surfaced: a dialog that wants
Esc-to-restore-focus must route its initial focus through the hook's `initialFocus`
parameter, **never** via a child's `autoFocus` — autoFocus fires at React commit, before
the hook's effect captures who had focus, so the hook would record the dialog's own input
as the "opener" and restore focus to a dead node.

### Command palette (#491)

`Ctrl/Cmd+K` toggles a keyboard-first overlay on every screen (`src/components/
CommandPalette.tsx`, mounted once in the Shell; a **Search… ⌘K** button in the side rail
opens it by pointer). It is wayfinding, **not a second API surface**: every entry comes
from state the shell already holds —

- **Conversations** — the `["sessions"]` query cache, recency-ordered (capped at 8 while
  browsing, uncapped under a query); picking one `openSession(id)` + navigates to `/`.
- **Pages** — `SURFACES` plus `modulePageNavs(modules)` minus `review` archetypes: exactly
  the rail's data, so new module pages appear with zero palette changes.
- **Actions** — *New chat*, *Wake up / Pause — unload models* (mirrors the PowerOrb's
  mutation on the `["power"]` cache), and *New note* when a notes editor page exists —
  a `?new=1` deep-link the editor archetype applies once, exactly like pressing its
  New-note button (the `?doc=` applied-guard pattern).

Typing filters every section through a dependency-free greedy subsequence scorer
(`src/lib/fuzzy.ts`: +2/char, +4 word-boundary start, +3 consecutive run, small
leading-gap penalty; ties keep recency/nav order). The input is a `role="combobox"` with
`aria-activedescendant` — focus never leaves it; arrows/Home/End move the active
`role="option"`, Enter runs it, Escape closes (capture-phase, the Confirm stacking
pattern) and `useModalFocus` restores focus. The dialog body mounts per open, so state
resets structurally rather than via effects.

### Toasts & confirmations (#488)

Native browser dialogs are **banned** in the shell — an ESLint
`no-restricted-syntax`/`no-restricted-globals` guard (the #394 pattern) rejects any
`window.alert`/`window.confirm` at lint time. In their place:

- **Toasts** (`src/stores/toasts.ts` + `src/components/Toaster.tsx`): a zustand-driven
  stack rendered once in the shell as flow children of the **CornerStack** (below), themed
  via the `--ep-*` tokens. Any code path raises one
  imperatively — `toast.error(msg)` / `toast.info(msg)` — no hook needed, so a mutation's
  `onError` can call it directly. Each card is a `role="status"` live region (polite
  announcement), closable by hand, auto-dismissed on a per-tone clock (errors 8 s, info
  4 s); re-raising an identical message replaces the card instead of stacking duplicates.
- **Confirmations** route through the shared `<Confirm>` primitive (`src/components/ui.tsx`)
  with `danger` styling for destructive actions — the editor's delete-file / delete-folder /
  restore-version prompts hold the pending action in state until the dialog resolves it.

**The corner region — `CornerStack` (#510).** Every bottom-corner transient — the toast
cards, the *new version ready* update prompt, the model **download tray** — renders as a
flow child of one positioned column (`src/components/CornerStack.tsx`): bottom-anchored
above the phone tab bar, bottom-right on wide screens, above the Confirm layer (`z-70`) so
a failure raised from a dialog action is never hidden. Corner surfaces must never pin their
own `fixed` box — independent fixed boxes at the same coordinates *occlude* (z-index picks
a winner and the rest sit invisible underneath) rather than stack; as column children, a
toast, the update prompt, and a download pill all show at once. Add any future corner
surface as a `CornerStack` child, never as a new `fixed` element.

### Offline & backend-unreachable banner (#494)

The PWA shell loads from the service-worker cache even when nothing behind it is
reachable — so silence is confusing, and on a LAN/VPN self-hosted setup (#460) "app up,
backend unreachable" is a *normal* state, not an edge case. The shell keeps **one
connection signal** (`src/stores/connection.ts`) with two inputs and renders it as a
quiet, moonlight-toned banner at the top of the main column:

- **offline — reconnecting** (`navigator.onLine` + the `online`/`offline` events): the
  device has no network at all. Wins when both signals fire — an offline phone also
  fails its probes, and "offline" is the truer story.
- **can't reach epicurus — retrying**: the device is fine but epicurus isn't answering.
  Evidence-based, with **no dedicated probe endpoint**: every `/platform` request flows
  through one fetch wrapper (`epFetch`, `src/lib/http.ts`), and each doubles as evidence
  — a network-level failure (fetch `TypeError`) or a gateway **502/504** (nginx up, core
  container down) marks it unreachable; **any** other answer marks it reachable (a 404
  or 500 proves the core answered, and **503 is excluded** — the LLM surface uses it for
  the *paused* state). The PowerOrb's existing 15 s `power` poll is the heartbeat that
  trips and clears the banner while the tab is visible; TanStack pauses that poll in
  hidden tabs, so a backgrounded PWA makes no extra requests.

Recovery is event-shaped (`useConnectionWatch`, mounted once in the Shell): the
browser's `online` event re-checks the vitals (`power`, `modules`) immediately;
returning to a visible tab while unreachable re-checks at once instead of waiting out
the poll; and when evidence flips back to reachable, the whole query cache is
invalidated **once per outage**, so screens showing outage-era data refetch instead of
quietly staying stale. The chat surface's own re-attach probe (#477) needs no wiring —
its `activeRun` calls flow through the same fetch, so a mid-turn outage feeds the same
signal it always fed.

While disconnected, the **composer keeps the draft** (it already persists) and disables
Send — button and Enter alike — behind a hint, instead of letting the message fail into
an error card for a reason the banner already explains.

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
label, an estimated size, a **fit badge** (#385 — the quant's estimated size judged against
your hardware via the same `assessFit`, so a smaller quant can read ✓ *Fits* where the default
build is ⚠ *Tight*), a **recommended** mark (the best quality that fits VRAM, from
`src/lib/quantVariants.ts`), and an `installed`/`current` badge — and pulling one reuses the
normal download flow. A manual tag box remains for non-library or HF models the lookup can't
enumerate. The model's **capabilities** (tools/vision/…) are a model-level fact, so they sit
once on the panel's read-only facts row as icon badges (#385), not repeated on every variant.

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
detail), `calendar` (month / week / agenda — with per-calendar **visibility toggles** listing
every *enabled* calendar (not only those with in-window events), events **tinted with their
calendar's colour** (the provider's own colour when it supplies one, else a stable derived
hue — dot and chips always match, #431), a **recurring event's repeat rule + guest list**
shown in its detail view (#432), and an
instant-paint **month cache** that revalidates in the background, #378/#379), `editor`
(Obsidian-like doc), and `board`
(columns of cards) all ship today. `browser` guards its folder rows against the duplicated
tap-navigation mobile PWAs can fire (a short debounce on folder taps plus a path-keyed list
remount, #428). Page data is fetched through the core proxy
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
filtering stay module-side while the shell stays a bounded renderer. Cards can also be
**dragged between columns** to move a task (#380): the drop reuses the card's *existing* move
action (its `to_list_id` picker), matched to the drop column by title, so the contract is
unchanged — it takes effect only where a column maps to a list (a drop on a due/status/priority
column is a no-op), with the action/form path as the pointer-free fallback.

The `editor` archetype (knowledge, notes) opens a document **rendered and editable** — its
markdown shows immediately as a **WYSIWYG** surface you type into directly (Milkdown's Crepe —
ProseMirror + remark — lazy-loaded so it never enters the main bundle, #377), and an Edit/Preview
toggle drops to the **raw markdown source** when you prefer it (ADR-0042). Both views write back
to the same markdown buffer, so the save/version flow below is unchanged. Because notes/knowledge **re-embed on every save**, the editor does not
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
file rendered as markdown, opened from the core-owned Files browser via
`GET /platform/v1/files/read` — the split-screen reader, #KB-refactor / ADR-0063). The panel
never runs module markup.

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

**Paste & drag-drop (#489).** Two more routes into the same upload endpoint: pasting a
clipboard that carries files (a screenshot, a copied file) into the composer textarea
uploads each one — plain-text pastes flow through untouched — and dragging files anywhere
over the chat column shows a themed **"Drop to attach"** hint (a depth counter keeps it
from flickering across child boundaries; non-file drags never trigger it) and uploads on
drop. In-flight uploads render as spinner pills (`PendingAttachmentPill`) beside the real
ones; failures surface as an error toast carrying the server's 413/415 size/type message,
so the limit messaging stays single-sourced with the picker's. While a **modal overlay** is
open (a Sheet, a Confirm, the review window — anything `aria-modal`), the drop surface is
**inert** (#511): a backdrop blocks clicks but not native drag events, so a drag used to pop
the hint and upload *underneath* the layer the user was looking at. Suppression is the
least-surprise call; the drop is still swallowed (never handed back to the browser, which
would navigate away to the file) and the drag cursor reads *not-allowed* while suppressed.

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
tool call's `running`→`ok`/`error`), `done` (the final `AgentTurn`), `error`, and
`awaiting_input` — the turn paused on a clarifying **`ask_user`** question (ADR-0053): the
stream ends carrying `{run_id, question}`, the shell renders the question with an inline answer
input, and answering posts to `POST /platform/v1/agent/runs/{run_id}/resume` to continue the
turn (the resume reply streams the same protocol). The pending question is persisted client-side,
so a refresh keeps the prompt while the suspended run stays durable server-side.

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
the text fields `TextInput` / `NumberInput` / `TextArea`, the styled `Select`, `Switch`,
`Sheet`, `Confirm`, `Tooltip`, and the exported `useModalFocus` hook — the modal keyboard
contract any hand-rolled dialog adopts, #512). **Every form control routes through these**, so none falls
back to the browser-default (white-bordered) control: `TextInput` / `NumberInput` / `Select`
all carry the one themed look — an `--color-edge` border on `--color-surface-2`, with `min-w-0`
so a native date/`datetime-local` picker or a select can't overflow a narrow mobile sheet
(#335). An eslint `no-restricted-syntax` guard rejects a **bare `<input>` / `<select>`** outside
`ui.tsx` (#394); a non-text input (range / file / checkbox) opts out with an `eslint-disable`
+ reason. A `no-restricted-globals` guard likewise rejects a bare `fetch(` outside
`src/lib/http.ts`'s `epFetch` (#494), so a new call site can't silently bypass outage
detection (#529). `Select` and `Button` both take `size="sm"` for a denser toolbar (compact inline filters /
view-controls, or a page-level `ActionControl` sized to match — #427) and `md` (default,
matching the text-field height) for forms; `Select`'s width is opt-in (`className="w-full"`).
`Tooltip` (#334) is a dependency-free hover/focus label for **icon-only** controls — the icon
keeps its `aria-label` and the wordy label moves into the tip; used by the turn-activity
summary, the board's compact "+" Add, the Files up-nav, the connected-account row's
credential / disconnect actions (#393), and the chat header's Conversations / New-chat
buttons (#480). `Switch` is the single on/off control used
everywhere (per-tool toggles, module enable/disable, boolean schema fields). Its **track
colour carries the state** — accent when on, muted when off — while the thumb stays a
constant, bright, evenly-inset circle that simply slides between ends. Keep that convention
so every toggle in the shell reads the same; the thumb must never change colour or sit flush
against the edge (that read as a dot escaping the pill, #245).
