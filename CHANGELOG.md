# Changelog

All notable changes to epicurus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`v0.1.0` is the first release — the first version usable on a server with a UI.

A release is cut by pushing a semver tag (`git tag v0.1.0 && git push origin
v0.1.0`); GitHub Actions then publishes the GitHub Release and versioned container
images to GHCR.

## [Unreleased]

### Fixed

- **Mail: paging pinned to the bottom, action row at the top + icon-only on phone** (#624, #626) —
  two mailbox UX fixes. The **Newer/Older** paging controls scrolled away with the message rows
  (attached mid-content); each mailbox view now owns its scroll, so the paging bar is a stable
  footer pinned to the bottom of the list (#624). The per-message **action row** (mark read/unread,
  archive, trash) sat at the bottom of the message and is now anchored at the **top**, and on a
  narrow viewport the buttons are **icon-only** — labels hidden from `sm:` down, with the
  aria-label + tooltip preserved so they stay named and accessible (#626). `web` 0.107.0→0.108.0.

### Added

- **Chat: edit any user message in history, regenerating from that point** (#552) — editing was
  limited to the **last** user message (#302), so fixing a typo three turns back meant retyping the
  conversation. Every user message now carries the same inline **Edit** affordance (revealed on
  hover/focus on earlier turns, the #480 per-turn pattern), opening #302's in-place composer.
  Editing keeps #302's decision — revise in place and truncate, never branch — so editing turn *k*
  discards turns *k+1..n* and streams a fresh answer under it. Because that throws away **real**
  content, a mid-history edit now **confirms with the count** ("removes the N later messages");
  editing the last message is unchanged and confirms nothing. `POST /sessions/{id}/edit` gains an
  optional `message_id` (absent ⇒ the last user message, so existing callers are untouched), and
  the transcript (`GET /sessions/{id}`) now carries each message's `id` as the anchor to name.
  Validation runs **before** any write — the anchor must be a **user** message of **this** session,
  and no turn may be running — so a rejected edit can never leave the conversation truncated. That
  ordering also fixes a latent bug inherited from #302: `/edit` revised and truncated *before*
  reaching the one-run 409, which for a mid-history edit would have destroyed real turns and then
  refused to re-answer. Nothing re-indexes the revised text — messages are not a recall corpus
  (ADR-0045), so `memory_search` reads the new text (and none of the discarded turns) straight from
  the store; the extraction queue (#326) is likewise unaffected, holding a **text snapshot** rather
  than a reference to a message that may no longer exist. `core-app` 0.78.0→0.79.0; `web`
  0.108.0→0.109.0.

- **Mail: mark message read on open** (#625) — a message stayed unread until acted on explicitly.
  Opening a conversation now marks its unread messages read at once: the shell flips the list row
  (and the messages' badges) **optimistically**, then calls a new operator-gated
  `POST /pages/mailbox/mark-read` in the background, which clears each message's flag at the
  provider (`set_unread`, the #277 seam) and **writes the read state through to the local cache**
  (ADR-0096) so the row converges immediately — no reconcile-race flicker — and reverts on a
  provider failure. Marking a whole thread read is the one case where a thread-level cache
  write-through is unambiguous (this gives #623's write-through primitive its first caller). `mail`
  0.12.0→0.13.0; `core-app` 0.77.0→0.78.0; `web` 0.106.0→0.107.0.

- **Core: maintenance schedule controls — on/off, cadence, time of day** (#621, ADR-0098) — the
  maintenance panel read "manual only — nightly schedule off" with no way to change it: the
  nightly trigger was a code-level default (`MAINTENANCE_SCHEDULE_ENABLED`/`MAINTENANCE_HOUR`),
  fixed for the process's whole lifetime, with no endpoint to edit it. Now a real, per-tenant,
  runtime-editable schedule — enable/disable, an hourly/daily/weekly cadence, an hour (+weekday
  for weekly), read in the tenant's timezone (ADR-0039) — governs the orchestrator's nightly
  batch as a whole (the job registry itself stays untouched and additive-only, so the incoming
  reflection job, #615, still rides this one shared hour per ADR-0093). Persisted per tenant
  (`maintenance_schedule_prefs`, the same settings-primitives shape as `timezone_prefs`/
  `page_order_prefs`); a missing row falls back to the env-configured default, so a fresh
  install behaves exactly as before. The orchestrator now **polls** every
  `MAINTENANCE_POLL_INTERVAL_S` (default 60s) and re-reads the schedule fresh each tick — a
  fixed `sleep_until_hour` computed once at wake couldn't react to a schedule changed while it
  slept. `GET /platform/v1/maintenance` gains `schedule_cadence`/`schedule_weekday`/
  `next_run_at` (an estimate for display); a new `PUT .../schedule` validates and persists the
  whole schedule at once (400 on an invalid shape, e.g. weekly with no weekday). The Settings
  panel grows enable/cadence/hour/weekday controls plus an effective-schedule + next-planned-run
  summary — a multi-field draft the operator edits and explicitly saves (auto-saving per field
  would fire invalid combinations mid-edit). `core-app` 0.76.0→0.77.0, `web` 0.105.1→0.106.0.

- **Models: show the max context window with the other model info** (#618) — the Models page
  badged each model's capabilities (tools/vision/…) but never its trained context window, so
  choosing between two similarly-named variants meant guessing. Local models now carry a compact
  "128k"/"1M"-style chip alongside the existing capability badges, sourced from the same
  `/api/show` call the capabilities already come from (opt-in, one call per model — the chat
  picker stays light). Hosted (saved) models get the same chip, sourced from LiteLLM's own
  model-cost map — the identical lookup #633 added for hosted vision/tool capabilities — always
  included since it's a static lookup, not a network call. Omitted, never a fake default, when
  the runtime/map doesn't report a length. `core-app` 0.75.0→0.76.0, `web` 0.104.0→0.105.0.

- **Chat: image input — vision models see the picture, end-to-end** (#633, ADR-0095) — attaching
  an image was silently mangled regardless of model: every `file` attachment was blindly
  `decode("utf-8")`'d into a text preamble, so a vision-capable model never received real pixel
  data, hosted or local. Now an image resolves separately (checked against the **stored**
  upload's real content-type) into multimodal content parts (`image_url`) spliced into the
  assembled turn just before the provider call — never into what gets persisted, so a stored
  turn never balloons with base64. Gated on the selected model's **actual** vision support
  (`gateway.supports_vision`) — stricter than the existing tool-capability check, since a
  mis-sent image either gets ignored or draws a provider 400: hosted models are checked against
  LiteLLM's own model-cost map (never assumed capable), and a local model with unreported
  capabilities defaults to **not** vision-capable. A non-vision model gets a clear explanation
  before any provider call, same shape as a normal answer, not a raw error. The same LiteLLM
  lookup also fills in **hosted-model context length + capabilities** for
  `GET /llm/models/details` (previously local-only), so the composer's "can't use tools"-style
  hint now works for hosted models too, alongside a new "can't see images" hint (advisory —
  Send still works; the server's own gate is the real enforcement). Web: the upload picker's
  accepted types now align with the server's #175 allowlist. `epicurus-core` 0.26.0→0.27.0,
  `core-app` 0.74.0→0.75.0, `web` 0.103.0→0.104.0.

- **Calendar: toolbar reworked into one stretched row; calendars picker clamped on mobile**
  (#628, #629) — the control row read as cramped and unbalanced; it now follows the shell's toolbar
  convention (the board's `gap-x-3 gap-y-2` bar) — a **Today · ‹ › · period** navigation cluster on
  the left, the page actions + **Calendars** picker + view switch pushed right by `ml-auto` so the
  row stretches the full width; icon-only "New event"/Calendars keep it to one line on all but the
  narrowest phones (where it wraps to a tidy second line). The **Calendars** visibility popover
  opened **partly off a phone screen**; it is now **clamped to the viewport** — positioned `fixed`
  from its trigger, shifted horizontally to stay on-screen, flipped above when there's more room up
  than down, and height-capped with a scroll — so every calendar is reachable regardless of trigger
  position. Calendar-local components only; no shared shell component changed. `web`
  0.102.0→0.103.0.

- **Calendar: tap a month day to open its week; slim event lines on phone** (#630, #632) — in the
  month view, tapping a day **navigated into a half-started create**; it now **opens that day's
  week view** (with the day highlighted), making the month a navigator and putting legible detail
  one tap away in the hourly grid. Event **creation** moves to the explicit affordances — the
  toolbar **New event** and the week grid's **empty-slot tap** (the #473 slot-seed create,
  relocated from the month cell to the grid where Google-style calendars put it). On a **phone**,
  a busy day showed a few blank-looking chips plus a `+2 more`; it now renders **every** event as a
  **slim textless colour line** (density over labels — the tap-through carries the detail),
  collapsing to a `+N` marker only past what genuinely fits. **Desktop** keeps the labelled chips.
  `web` 0.101.0→0.102.0.

- **Calendar: week view is now an hourly day-grid with drag-to-move** (#631) — the week view was a
  plain per-day list of event cards; it is now a Google-Calendar-like **hourly grid**: one column
  per day over hour rows, timed events **placed and sized by start/duration**, overlapping events
  **split into side-by-side lanes**, a **pinned all-day strip** (ADR-0037) that stays put while the
  hours scroll, a **current-time line**, and a default scroll to the morning. A timed event on a
  writable calendar is **dragged to move** (or its bottom edge dragged to **resize**), snapped to the
  quarter-hour and applied **optimistically**; the write goes through the event's *own* editable-
  calendar **Edit** action (`calendar_update_event`, #208/ADR-0034) — the same tool the Edit form
  calls, so **no module contract changes** (the module supplies data, the shell renders, ADR-0018) —
  and rolls back with a dismissible message on provider failure. A read-only event stays
  click-to-open. On a phone the grid **pans horizontally** with the time gutter and day headers
  pinned. Placement and drag maths are framework-free and unit-tested
  (`services/web/src/components/archetypes/calendarGrid.ts`). `web` 0.100.0→0.101.0.

- **Mail: render HTML email properly (images, styling)** (#627, ADR-0097) — HTML mail rendered
  badly: the shell decoded every message to plain text (no images, no layout), because rendering
  raw mail HTML in the shell would be an XSS surface. The module now surfaces the message's
  **`body_html`** (plus inline images' `Content-ID`s, marked `inline`), and the shell renders it in
  a **sandboxed iframe** (`allow-same-origin allow-popups`, **never** `allow-scripts`) — so email JS
  can never run and the email's CSS can't bleed into or restyle the app shell. Two independent safety
  layers: the HTML is first sanitized by an **inert `DOMParser` pass** (strip `<script>`/`<link>`/
  `<iframe>`/`<form>`, every `on*=` handler, `javascript:`/`vbscript:` URLs) — the raw HTML never
  touches the live DOM — then the sandbox neutralizes anything missed. Inline **`cid:` images** are
  rewritten to the module's same-origin attachment proxy (fetched through the module, never a direct
  provider URL), so they load with the session cookie; inline images are kept out of the download
  row. **Remote images are blocked by default** (a remote `<img>` is a tracking pixel) with a
  per-message "Load images" affordance — the deliberate privacy default. Plain text stays the
  fallback for text-only mail and the `mail_read` tool; emails render on a white canvas in both app
  themes (the mainstream mail-client convention) for legibility. `mail` 0.11.0→0.12.0; `web`
  0.99.0→0.100.0.

- **Mail: local cache + incremental sync — stop full-fetching on every open** (#623, ADR-0096) —
  the mailbox page fetched everything from Gmail on *every* open (the rail + one metadata
  `threads.get` per thread, ~28 calls for a 25-row page), so opening Mail was slow. The module now
  keeps a tenant-scoped **local cache** (`mail_thread`/`mail_label`/`mail_sync`/`mail_landing`, owned
  by the mail service — no shared DB) and reconciles it incrementally. The plain landing view serves
  the cached rows + rail with **no** provider call (the first open of a folder is a one-time cold
  sync; every open after renders in ~a second); the web then fires a background `?reconcile=1` read
  that pulls **only the delta** — via a provider-neutral change cursor (`MailCursor {history_id,
  uid_validity, uid_next}` behind the `MailProvider` seam; Gmail uses `historyId` +
  `users.history.list`, IMAP's `UIDVALIDITY`/`UIDNEXT` reserved) — rebuilding only the touched thread
  rows so new/changed messages, read/unread flips, and archives appear without a manual refresh. A
  cursor too old to replay (Gmail expires history after ~a week) or an IMAP `UIDVALIDITY` rotation
  triggers a full resync. Read/unread converges both ways (a mark-read writes through to the cache
  optimistically). Search (`?q=`) and deeper pages (`?cursor=`) still read live — the cache only
  accelerates the landing open. Large-int columns are `BigInteger` (a Gmail `historyId` and the
  epoch-millisecond `sort_ts` ordering key both exceed int32); the schema evolves via `create_all` +
  the shared additive `ensure_columns` reconcile (ADR-0067). `mail` 0.10.0→0.11.0; `web`
  0.98.0→0.99.0 (the mailbox view's cache-then-reconcile flow).

- **Agent: loop hygiene — stop on repeated calls and error streaks** (#524, ADR-0091) — the step
  loop continued on the blunt rule "the model made a tool call", so two shapes burned the whole
  `max_steps` budget and ended in a silent stop: the model re-issuing the *exact same* call over and
  over, and a streak of tool errors (retrying a broken call to exhaustion). A small `_LoopGuard`
  wraps the loop (ADR-0001 stays thin — outcome-aware *stopping*, not planning), applied identically
  to `run` and `run_stream`. A repeated identical call (canonicalized `(name, sorted-args)`, so
  distinct-args paging/per-item repeats pass untouched) earns a one-shot nudge and is **not
  re-executed** — a repeated write would double-apply — then ends the turn `stopped="repeat_call"` on
  a further repeat; three consecutive tool errors end it `stopped="tool_errors"` (any success resets
  the streak). Both take the same single tool-less final round `max_steps` already uses, so the turn
  ends with a **real answer** — "here's what I found / what failed" — never a silent stall. The new
  `stopped` reasons ride the streamed `done` event for the web to key copy off; the repeated/errored
  steps stay visible in the activity timeline. (Web stop-reason badge deferred — the transcript
  carries no per-message `stopped` yet; the live reason is already on the `done` event.)
  `core-app` 0.73.0→0.74.0.


- **Agent: `memory_search` built-in tool — deliberate recall over past sessions and facts**
  (#523, ADR-0089) — structural recall only injects top-k facts every turn, with no way for the
  agent to *dig*; "what did we decide last week about the backup strategy?" failed unless
  extraction happened to distil that exact decision into a fact. A new core built-in
  `memory_search(query, scope = facts | sessions | both, limit)` closes the gap: the agent
  deliberately searches the durable **fact store** (Qdrant, the same ranking a turn's ambient
  recall gets) **and** past **conversations** (a portable case-insensitive content match over
  `agent_messages`, joined back to each conversation's title), and gets back a compact, capped
  set of the most relevant facts + past-conversation excerpts. It registers alongside
  `now`/`remember`/`ask_user` (ADR-0039) and shows as a normal step in the activity timeline.
  Tenant-scoped on every query (recall crosses sessions, so scoping is a privacy boundary,
  constraint #1); best-effort like the rest of memory — the facts half embeds through the
  gateway (constraint #8), so a cold embedder degrades to just the sessions text search (no
  embed) rather than failing the tool call. Results are capped (≤10/source, snippets trimmed)
  for token discipline — never a raw session dump. `core-app` 0.72.0→0.73.0.

- **Agent: scheduled turns — recurring prompts that run unattended** (#526, ADR-0092) — the
  agent was purely reactive, needing an HTTP caller or an inbound bridge message for every
  turn; an operator can now author a prompt with a daily/weekly cadence at a local hour, and
  it fires on its own, delivering into its own chat session (a fresh session that comes into
  being — titled from the prompt itself — the moment it first fires). A single background poll
  loop (not one wake-at-hour task per row, since each row carries its own independently
  configured hour and rows are created/paused/deleted at runtime) finds due rows each tick and
  runs them sequentially through the normal headless-turn path, metered under the row's own
  tenant. Respects power state: a paused runtime skips a due row and records the skip (not a
  silent no-op, and never a burst of catch-up runs on resume). Settings-surface only (no module
  UI) — a new **Scheduled turns** card: list, create (prompt + cadence + hour + weekday),
  enable/disable, delete. `core-app` 0.70.0→0.71.0, `web` 0.96.0→0.97.0.

- **Memory: a standing user profile, synthesized nightly and injected statically** (#527, ADR-0094)
  — recall was top-k vector search over the fact store at *turn time*, so the stable common case
  (who the user is, durable preferences) paid an embedding round-trip and a single-GPU model-swap
  risk on **every** turn, for content that barely changes. Now a nightly `profile_synthesis_job`
  (added to the ADR-0060 maintenance registry, beside the extraction drain) distils each tenant's
  facts into one compact **standing profile** via a single gateway call, and `Agent._assemble`
  injects it as a static system block with **no turn-time embed** — moving the common-case cost off
  the response path (the same trade ADR-0051 made for fact extraction); vector recall stays for the
  long-tail specifics. Stored per tenant and versioned (`standing_profiles`, last 5 kept). Visible,
  **editable, and clearable** in the Settings → Memory view (`GET/PUT/DELETE /memory/profile`): an
  operator edit is **pinned** and survives re-synthesis (the synthesizer skips a tenant whose
  profile is `edited`) until cleared. Best-effort throughout — no profile is exactly today's
  behavior, a failed synthesis keeps the previous profile. Nightly auto-runs ride the opt-in
  maintenance schedule (`MAINTENANCE_SCHEDULE_ENABLED`) or the manual "run everything" trigger.
  `core-app` 0.69.0→0.70.0, `web` 0.95.0→0.96.0.

- **Review: edit the draft before approving, with an audit trail** (#542, ADR-0090) — review
  surfaces (knowledge's Suggestions, notes' Note suggestions) were approve/reject only; the
  operator can now hand-edit the proposed content directly in the review window before
  approving — "edit anywhere before approving anything" — layered on top of the existing
  per-hunk merge. Approve carries the edited draft back to the module, which writes what was
  actually approved. Every approve/reject now also records an audit row (the agent's original
  proposal alongside what was actually applied), visible in a **Recently resolved** panel
  under the pending queue — the pending queue itself still holds only unresolved suggestions
  (ADR-0033), so this is the durable trail that survives resolution. The wire contract
  (`ReviewSuggestion`/`ReviewData`/`ApplyResult`/`ApproveBody`/`ReviewDecision`/
  `ReviewAuditData`) moves into a shared `epicurus_core.review` module so every review-page
  adopter — knowledge and notes today, governed playbooks (#525) next — gets
  edit-before-approve and the audit trail for free instead of reimplementing it. `epicurus-core`
  0.25.0→0.26.0, `knowledge` 0.21.0→0.22.0, `notes` 0.6.0→0.7.0, `core-app` 0.68.0→0.69.0, `web`
  0.94.0→0.95.0.
- **Mail: a full mail client in the shell** (#550, ADR-0087) — mail becomes a first-class
  left-nav page like Files / Calendar / Tasks / Notes, through a new **`mailbox` page
  archetype**: a labels rail with unread counts → a cursor-paginated thread list → the full
  conversation, with **compose and reply**. Browsing is folder-scoped; a search (`q`, Gmail
  syntax) spans the whole mailbox, and the page size is capped so one fetch can't scan an
  unbounded mailbox (#539). The mail module ships **zero markup** (ADR-0018): it declares the
  archetype and supplies data, and the core shell renders — reusing the *same* `MailMessageView`
  the panel `email-reader` already uses, not a fork. **Plain-text-first**: an HTML-only message
  is decoded to readable text server-side (`_html_to_text`, adversarially tested), so no HTML is
  ever rendered in the shell — zero mail-XSS surface. `mail` 0.9.1→0.10.0, `core-app`
  0.66.3→0.67.0, `epicurus-core` 0.24.0→0.25.0, `web` 0.90.0→0.91.0.
- **Calendar: show task due-dates on the calendar page** (#469, ADR-0088) — "what's on my
  plate today" meant checking the tasks board and the calendar separately; open tasks with a
  due date now show as read-only, checkbox-glyphed chips on their due day, distinct from real
  events. The core gains a new cross-module **calendar-feed** aggregate
  (`GET /platform/v1/calendar-feed?start=&end=`, `ModuleRegistry.calendar_feed_items`) —
  deliberately **not** a manifest-declared capability like `resolver`/`attachable`: a module
  opts in purely by serving `GET /calendar-feed?start=&end=` itself, and the aggregator
  probes every enabled, healthy module for that path, skipping a 404/unreachable one exactly
  the way the existing `/suggestions` feed already tolerates a down module — reusing that
  pattern kept every line of this change inside `services/tasks` and `services/core-app`,
  with zero touches to the shared `libs/epicurus-core` (which was under concurrent edit for
  an unrelated in-flight archetype at the time). `tasks` is the first module to implement it:
  `calendar_feed_items` filters the already-fetched open-task list to a due date in range,
  carrying each item's own status (open vs. in-progress) and a `kind` field (beyond the
  issue's own sketch) so the shell's click handler can call the *generic* hover-card resolver
  without hardcoding "task". Every task hover-card also gained a link back to the Tasks board,
  not only ones reached from the calendar. Verified live: a task chip renders on its due day
  alongside real events, resolves through `GET /resolve/task/{ref_id}` and opens in the right
  panel on click, and a failed feed fetch never blanks the events that did load. Month-grid
  view only for this pass — week/agenda are a follow-up, mirroring #473's own scoping of the
  time-grid slot-click to "no hour-grid view exists yet." `tasks` 0.15.3→0.16.0, `core-app`
  0.67.1→0.68.0, `web` 0.93.0→0.94.0.
- **Calendar: click a day to create an event, pre-filled** (#473) — creating an event meant
  reaching for the toolbar's "New event" button and re-typing a date the calendar was already
  showing. Clicking empty space in a month-grid day cell now opens the page's own existing
  create-event form (ADR-0034 — no new module contract) pre-filled with that date, `all_day`
  on by default so the date pickers collapse correctly (`date_toggle`, #252); the operator can
  still adjust or switch to a timed event before submitting. Event chips and the "+N more"
  overflow button keep opening event/detail as before — each stops the click from also
  bubbling into the new cell handler, so only genuinely empty space triggers create. The
  existing calendar-picker default (`form_values.calendar_id`) survives untouched, since the
  seed only ever adds `start`/`end`/`all_day` on top of it, never replacing the whole set.
  Two small, generic extensions carry this rather than a calendar-specific one-off:
  `ActionControl` gains optional `initialValues` (merged over a module's `form_values`) and
  `open`/`onOpenChange`/`hideTrigger` (drives its form sheet from outside, with no visible
  button of its own) — any future archetype wanting the same "click something else to open
  an existing form, pre-filled" pattern reuses it as-is. The day/time-grid slot-click and
  click-drag range-select the issue also describes has no substrate yet — `WeekView` is a
  per-day list, not an hour grid — and is left for whenever that view exists; the acceptance
  criteria's own "(where those views exist)" qualifier anticipated the gap. `web` 0.92.0→0.93.0.
- **Websearch: results as Sources-pill chips, at parity with local sources** (#551,
  ADR-0019) — a `web_search` answer previously left the operator unfolding raw tool-call
  JSON to see which pages informed it; local sources (knowledge/mail/calendar/tasks) had
  this solved since #333. `web_search` now returns a `ToolEnvelope` (text unchanged —
  still a ranked title/URL/snippet listing the model can cite — plus one `EntityRef` per
  result), so results render as the same **Sources (N)** pill and chips. The module stays
  **stateless**: a new `epicurus_websearch.refs` codec (mirroring
  `epicurus_knowledge.refs`'s self-describing-id pattern) base64url-encodes each result's
  url/title/snippet/engine directly into its `ref_id`, so the new `GET
  /resolve/result/{ref_id}` hover-card resolver reconstructs everything with no store —
  hover-cards keep resolving in a session reopened long after the search ran. Because
  websearch has no right-panel view of its own, its chip is the first to diverge from the
  generic click-opens-the-panel behavior: it always carries an `href` and a chip click
  resolves then opens the source page directly in a new tab
  (`rel="noopener noreferrer"`), with an external-link glyph on the chip itself so a web
  source is never mistaken for an in-app entity — both already-generic frontend pieces
  (`CardLink`'s scheme-gated external-link branch, the core's cross-call `_RefCollector`
  ref-id dedup) needed zero core-app changes to support this end-to-end. Same-page
  duplicates within one search (SearXNG returning a URL from multiple engines) are
  collapsed before refs are built; a malformed or tampered `ref_id` (bad base64, non-JSON,
  or a non-`http(s)` scheme) 400s cleanly, never a 500 and never an unsafe `href`.
  `websearch` 0.1.0→0.2.0, `web` 0.91.0→0.92.0.
- **PWA: share target + app shortcuts** (#493) — the installed app was inert to the OS around
  it. Two manifest-level features, especially useful on Android: **share a link, text, or
  file/photo from any app straight into a chat turn** (`manifest.share_target`, `POST`
  `multipart/form-data`), and **long-press the icon → New chat / Calendar / Tasks**
  (`manifest.shortcuts`). The share target needed a service worker that can intercept a POST
  before the browser discards its body navigating away — something `vite-plugin-pwa`'s
  auto-generated `generateSW` service worker has no way to express — so the service worker is
  now a custom source file (`src/sw.ts`, `injectManifest` strategy) that reproduces the SPA
  navigation fallback and the `registerType: "prompt"` update-skipWaiting wiring the old
  declarative config gave for free (its own top comment explains both), plus the new
  share-target `fetch` handler: it stashes the shared `title`/`text`/`url`/file in the Cache
  API and 303-redirects to `/?share=1`; the chat screen prefills the composer (appended to a
  draft already in progress, never clobbering it — the composer never sends on the operator's
  behalf, the #480 starter-chip principle) and uploads any file through the same path a
  paste/drop already uses. The Calendar/Tasks shortcuts reuse `ModulePageScreen`'s existing
  "no such module page" empty state as their degrade when that module is off — no new code
  needed there. `src/sw.ts` is excluded from `tsconfig.json` (a service worker's `WebWorker`
  lib can't coexist with this project's `DOM` lib in one project) and from the `no-restricted-
  globals` bare-`fetch` guard (#529) — it is its own global scope entirely, with no `epFetch`
  to route through. `vite preview` also gained its own `proxy` block (it doesn't inherit
  `server.proxy`), since a production build is the only way this whole surface ever runs —
  verified live against one: the service worker registers and activates, a real POST to
  `/share-target` is intercepted and redirects correctly, and the chat screen consumes the
  staged payload end-to-end. `web` 0.89.0→0.90.0.
- **Chat: a "finished while you were away" indicator for background turns** (#492) — a turn
  outlives its connection (#376) and other sessions can generate concurrently (#396's pulsing
  dot shows that *while* it runs), but nothing told the operator once a background turn
  *finished* — the answer just sat there until they happened to reopen that conversation. A
  shell-level watcher (`useAwayFinishedWatch`, mounted once in `Shell()` alongside the existing
  `useConnectionWatch`) polls the same `["active-runs"]` query `SessionsSheet` already used
  (gated on being open) — now always live, a steady 15 s, regardless of which screen is
  showing — and diffs each poll against the previous one: a session that *was* active and no
  longer is, and isn't the one currently open, just finished unseen. One boolean marker per
  session (no counts, no push notifications — those are Phase-4/bridges territory): the
  session's row in the Conversations sheet gets a static accent dot + a bolder title, the chat
  header's History button picks up the same dot plus an `aria-label` update, and the document
  title gets a `•` prefix so a backgrounded tab/PWA shows it too. All three clear the instant
  that session opens — funneled through the one `openSession` store action every entry point
  (sheet row, palette, hover-card) already calls, so nothing needed wiring per call site. No
  extra polling while the tab is hidden — inherited for free from React Query's default
  `refetchInterval` behavior, verified live (the watcher's poll visibly stalls while
  `document.visibilityState` is `"hidden"` and resumes the moment it flips back). `web`
  0.88.6→0.89.0.

- **Maintenance: live progress + refresh-proof batches** (#561) — running maintenance showed no
  progress beyond a spinner, and refreshing the page during a batch lost it entirely (the card's
  running state was pure client mutation state). The batch itself was never at risk — verified
  empirically (a real uvicorn server, a client disconnecting mid-request both gracefully and via a
  hard reset) that this stack does not cancel a plain in-request `await` on client disconnect — but
  the batch still had no way to be *observed* after a refresh, and nothing stopped a second manual
  trigger (or an overlapping nightly window) from racing a duplicate batch. `POST /run` now starts
  the batch as a **detached background task** (the same shape as chat turns, `agent/live_runs.py`,
  #376) and returns **202** immediately with its live progress instead of holding the request open
  for however long a full re-embed takes; the orchestrator tracks a `current_run` with per-job
  `pending`/`running`/`ok`/`skipped`/`error` status as it sequences, exposed by `GET` alongside the
  last *completed* run. A second `POST` while one is live responds **409** rather than double-running
  — the nightly scheduler treats the same conflict as a benign skip — and `shutdown()` cancels an
  in-flight batch cleanly at app teardown instead of orphaning it against infra about to close. The
  Settings **Maintenance** card renders per-job progress from `current_run`, rehydrates onto it on
  mount (a refresh mid-batch lands back on the same run), and polls a few seconds apart while one is
  live. `core-app` 0.65.0→0.66.0, `web` 0.87.0→0.88.0.

- **Models: a context budget for hosted models — long chats compact instead of overflowing** (#570) —
  a saved hosted/API model had no context-window control and no compaction path: both readers of the
  per-model setting sat behind an `is_local` guard, so a long conversation grew until the provider
  rejected the turn with `context_length_exceeded`. Hosted rows now take the same per-model context
  setting local models already have (#289/#328), read as a **compaction budget** rather than an Ollama
  `num_ctx` — the size the history is trimmed to fit before the call, giving both overflow protection
  and a per-turn input-spend cap. Resolved by **exact model id only** (never the global Ollama pref,
  never the loose local-family match); an unset budget leaves behavior identical to today. `core-app`
  0.64.1→0.65.0, `web` 0.86.1→0.87.0.

- **Models: real GB sizes everywhere + honest cloud-only rows** (#571) — the model browser
  never showed a download size (the library *index* the catalog parses publishes none, so
  `size_gb` was seed-only and blanked after the first live refresh), and **cloud-only** models
  (`deepseek-v4-flash` — one upstream `cloud` tag, no weights) rendered as bare rows with a
  plain **Pull** that couldn't do what it said. Now the per-family **tags page** — the same
  page the quant-variant lookup (#330) already fetches — supplies real sizes end to end:
  `ModelVariant` gains `size_gb` (the pick-list shows exact per-quant sizes and judges fit and
  the *recommended* mark by them, estimates only as fallback), and a **background size fill**
  backfills catalog rows most-popular-first, **one rate-limited lookup per
  `LLM_CATALOG_SIZE_FILL_SECONDS`** (default 30 s; 0 disables) through the lookup's new
  per-family TTL cache — the catalog refresh itself stays **exactly one** upstream request,
  enriched sizes carry across refresh swaps, sized rows take their bare tag's size and
  size-less downloadable families (embedding models) take `latest`, and any tags-page failure
  just leaves that family size-less. On-demand variant lookups piggyback their sizes onto the
  catalog immediately. The tag vocabulary gains **`thinking`** (chip only) and **`cloud`** on
  *both* sides of the seam; `cloud` applies only to a pill-marked family's **size-less bare
  entry** (hybrids like gemma3/gpt-oss keep their downloadable rows untagged — the pill has
  *no* `x-test-capability` hook upstream, so the parser matches the pill span itself, verified
  live 2026-07-09). Cloud rows are **badged with the reason on hover/touch, offer no Pull, and
  show no fit verdict — by design**; cloud aliases in the variant list are labelled `cloud`
  and never given an estimated size. `core-app` 0.62.0→0.63.0, `web` 0.84.0→0.85.0.

- **Files: upload from the Files page — with a mobile source menu** (#479) — the Files page
  could browse, move, rename, and download, but nothing could be *put in* from the UI. A new
  core endpoint (`POST /platform/v1/files/upload?dir=`) lands one file per request through the
  FileStore seam (local-FS ↔ S3, constraint #3), tenant-scoped, **indexed immediately** so it's
  listed and searchable with no rescan, and bounded by the **shared #175 caps** (`ATTACHMENT_MAX_BYTES`
  → 413, `ATTACHMENT_ALLOWED_TYPES` → 415; nginx's `/platform/` 12 MiB body cap already fronts it).
  A name collision suffixes (`photo-2.jpg`) rather than overwrites; module-owned destinations are
  refused. The web's Files toolbar gains **Upload into the current directory**: phones get a
  Telegram-style bottom-sheet **source menu** (Photo or video → gallery, Camera → capture,
  Document → file manager), wide screens go straight to the file dialog, and the listing accepts
  **external file drops**. Multi-file picks upload sequentially with per-file progress pills —
  a rejected file pins the server's own 413/415 detail and raises a toast — and the listing
  refreshes per success. Movability in the Files view now follows the real ownership rule:
  **operator-space files are movable like object uploads; module-owned subtrees (the module
  hostnames — `knowledge/…`, `notes/…`) and directories stay read-only.** `core-app`
  0.60.0→0.61.0, `web` 0.82.0→0.83.0.

- **Cmd+K command palette** (#491) — the wayfinding capstone on #480: one keyboard-first
  overlay over everything the shell already knows. Ctrl/Cmd+K toggles it on every screen
  (a "Search… ⌘K" affordance in the side rail opens it by pointer); typing fuzzy-filters
  conversations (recency-ordered, from the sessions cache), core surfaces + module pages
  (the same registry data the rail renders), and a few actions — New chat, Wake/Pause,
  and New note when the notes module is installed (a `?new=1` deep-link that opens the
  editor's create flow). Arrows + Enter navigate, Esc closes and restores focus (#487
  contract, combobox semantics). Deliberately not a second API surface: the palette only
  reuses queries the shell already holds; the fuzzy scorer is a dependency-free
  subsequence ranker in `src/lib/fuzzy.ts`. Also fixes the calendar event-chip hover
  pairing `text-canvas` with a runtime calendar colour (#531): the hovered chip's text
  colour is now computed per colour (house ink → white → pure black, first to clear
  WCAG AA — `src/lib/color.ts`), so a light calendar on the light theme no longer washes
  the label out. `web` 0.81.0→0.82.0.

- **Web: fetch-guard lint rule + connection-gate regenerate/edit/resume** (#529, #530) — two
  follow-ups from the #519/#494 outage-detection review. (1) A `no-restricted-globals` rule
  (the same mechanism already banning `alert`/`confirm`, #488) now rejects a bare `fetch(`
  anywhere in `src` outside `src/lib/http.ts`'s own `epFetch`, so a future call site can't
  silently bypass the outage detector. (2) `regenerate()`, `saveEdit()`, and the `ask_user`
  resume-answer submit were the three remaining send-adjacent actions that still fired while
  the core was unreachable and failed into the generic error card instead of the composer's
  existing gate; all three now bail on `connectionLost` and disable their buttons the same way
  Send does, reusing the existing hint pill — no new UI. `web` 0.80.1→0.81.0.

- **Tasks: overdue recurrence sweep** (#515) — a recurring task nobody ever completed used to
  sit overdue forever (materialization was on-complete only). Every read (`tasks_list`, the
  board) now also materializes a fresh instance for an open, overdue recurring task: the
  overdue task itself stays open and untouched — only its rule retires, moving the recurrence
  to a new successor (skip-missed, like a late completion). Also: a materialize failure (next-due
  computation, successor creation, or rule retirement) is logged and never breaks the
  completion/read that triggered it, with one retry on the retire write before giving up;
  `tasks_update` now rejects setting `repeat` on a task with no due date (matching `tasks_add`);
  and the shared board `SchemaForm` now sends an explicit clear for an optional field that had
  a value and was blanked — on a task, "Does not repeat" over an existing rule actually clears
  it now instead of being silently dropped. The **calendar** edit form deliberately ignores a
  blanked repeat picker for now (`""` means "leave the series unchanged", the pre-existing
  behaviour): calendar has no clear-recurrence contract yet, and passing the blank through
  would reach Google as a bare `RRULE:` (API 400). `tasks` 0.14.0→0.15.0, `calendar`
  0.14.1→0.14.2, `web` 0.80.0→0.80.1.

- **Bound the entity-ref id block and a module's list text for large results** (#468,
  ADR-0084) — a large ref list (a wide search, RRULE-expanded calendar events over a long
  window, #443) previously echoed every ref's id into the model's context uncapped, roughly
  doubling an already-large listing's cost (ADR-0079). The core's entity-ref id block now
  truncates past `LIST_CAP` (50) refs with a "showing 50 of N — narrow the query/range or
  ask for more" note, logged with the tenant id — the full ref list still reaches the UI's
  chips unchanged. A new shared `epicurus_core.capped_listing` helper lets a module cap its
  own hand-built "Found N ...:" text the same way; `calendar_list_events` adopts it as the
  first caller. `epicurus-core` 0.22.0→0.23.0, `core-app` 0.59.0→0.60.0 (both MINOR — flag
  a version-line collision at merge time against other in-flight core-app PRs), `calendar`
  0.14.0→0.14.1 (PATCH).

- **Editable assistant system prompt — and a real base prompt at last** (#497, ADR-0083) — the
  agent ran with **no** base system prompt: its identity and behaviour were emergent from the tool
  schemas and the model's own defaults. This introduces the mechanism *and* the editor. A
  tenant-scoped prompt (new `agent_instructions` table, following the timezone-pref pattern) is
  injected as the **first** message of every turn — chat and headless bridge turns alike — ahead of
  recalled memory and attached context, where the compaction leading-prefix rule protects it from
  being trimmed. It's resolved per turn, so edits apply on the next message with no restart.
  `GET`/`PUT /platform/v1/agent/instructions` back a new **Settings → Assistant instructions** card
  (a textarea prefilled with the effective prompt, Save, Reset to default, and a soft-size warning —
  the prompt counts against every turn's context and is never trimmed). A shipped default
  establishes who epicurus is, a concise and candid voice, and tool-use discipline (with no
  date/time baked in — the `now` tool owns that). **Behaviour shift for existing installs:** with no
  stored prompt, every turn now gains the default preamble where before there was none. `core-app`
  0.58.0→0.59.0, `web` 0.79.0→0.80.0.

- **Hosted/API model ids you enter are now saved per tenant** (#496) — a hosted model typed into
  the chat picker (e.g. `claude/<model-id>`) used to live only in the browser (`recentModels`,
  capped at five, per device *and* per origin): come back from another device, a VPN-hostname
  origin, or after a PWA reinstall and it was gone. The core now persists the ids the operator uses
  in a tenant-scoped `saved_models` table, behind `GET` / `POST` / `DELETE
  /platform/v1/llm/saved-models`. The chat picker renders that server list as pick rows and
  **auto-saves on use** (the free-text box stays for one-off / new ids); the Models page lists them
  under each provider, where they can be **removed** or **set as the global default** (the star
  local models already had); and they're now assignable to a **module model slot** (ADR-0029).
  Saving rejects anything that isn't a *hosted* id — a known `<provider>/` prefix — which also
  fixes the client's old `includes("/")` heuristic that mis-filed a local `hf.co/org/model:tag` as
  hosted. `core-app` 0.57.1→0.58.0, `web` 0.78.0→0.79.0.

- **Web: offline / backend-unreachable banner** (#494) — the PWA now says when the backend can't
  be reached instead of failing silently. A transport-level detector (`epFetch`, wrapping every API
  fetch site) marks the core unreachable on network errors and 502/504 — 503 is deliberately
  excluded (a paused house is not an outage) — and any healthy response clears it. PowerOrb's
  existing 15 s power poll doubles as the heartbeat, so there is no new polling (and none while the
  tab is hidden). A moonlight banner appears (offline wording wins when the device itself is
  offline), the composer keeps the draft but gates Send, and recovery refetches vitals and
  invalidates queries once per outage. `web` 0.77.0→0.78.0.

- **Web: AA accent fills, one notification corner, drop gating, EventDetail focus**
  (#505, #510, #511, #512) — four overlay-polish fixes in one pass. A new
  `--ep-on-accent`/`--ep-accent-hover` token pair gives every accent-filled control an AA-passing
  label and hover fill in both themes and both power states, asserted by `contrast.test.ts` against
  the live CSS (the light "paused" label is white — the issue's ink estimate computed to 4.25:1).
  Toaster, UpdateToast, and DownloadTray now stack in one fixed `CornerStack` column instead of
  overlapping (rule: never add a new fixed corner element). Drag-drop attach is suppressed while
  any `aria-modal` overlay is open — `dragover` still `preventDefault`s so the browser can't
  navigate away. And the calendar's EventDetail overlay adopts the shared `useModalFocus` trap.
  `web` 0.76.0→0.77.0.

- **Recurring tasks + a friendly repeat picker** (#471, ADR-0082) — tasks can now **repeat**, on
  both providers, even though the Google Tasks API has **no recurrence field** (repeat is UI-only).
  A task carries an optional RRULE; **completing it materializes the next instance** with the next
  due date and retires the rule on the completed one, so the recurrence lives on exactly one open
  task at a time (re-completing can't double-fire; a `COUNT`/`UNTIL` series ends cleanly). The rule
  is stored per provider — a `repeat` column on the local row, a module-owned `task_repeats` side
  table keyed by task id for Google — but materialization is provider-agnostic (in the
  `TasksRouter`). The next due date uses a **skip-missed** policy (a late completion rolls forward
  to the next *future* occurrence). `tasks_add`/`tasks_update` gain a `repeat` parameter; the board
  card shows a *Repeats weekly* badge. The web form renders `repeat` — and the **calendar's**
  `recurrence` field, replacing its raw RRULE box — as a shared **friendly repeat picker** (None /
  Daily / Weekdays / Weekly / Monthly / Yearly / Custom…) via a new `format: rrule` form widget; the
  agent tools still accept a raw RRULE. Google caveats accepted explicitly: the rule is invisible in
  Google's own UI, a task changed directly in Google is reconciled on our next refresh, and deleting
  it in Google retires the rule (GC on miss). `tasks` 0.13.0→0.14.0, `calendar` 0.13.0→0.14.0,
  `web` 0.75.0→0.76.0.

- **Web: paste & drag-drop attachments in the chat composer** (#489) — pasting a screenshot
  or file from the clipboard into the composer, or dropping files anywhere over the chat
  column, now attaches them exactly as the AttachMenu picker would: same
  `POST /platform/v1/agent/attachments` endpoint, same pill, same server-sourced 413/415
  size/type messages (surfaced as an error toast). Text pastes flow through untouched; a
  themed "Drop to attach" hint appears only for real file drags (a depth counter stops
  enter/leave flicker across child boundaries, and in-app drags never trigger it); in-flight
  uploads show spinner pills; multi-file drops upload every file. On a PWA whose main
  surface is chat, paste-to-attach was the highest-QoL missing interaction. `web`
  0.74.0→0.75.0.

- **Web: overlay focus management for Sheet/Confirm** (#487) — the two overlay primitives
  declared `role="dialog"`/`aria-modal` but had no focus handling at all: on open, focus
  stayed behind the backdrop; Tab walked the page underneath; closing dropped focus on
  `<body>`. A shared `useModalFocus` hook (hand-rolled, dependency-free) now gives both the
  full keyboard contract: on open, focus moves into the dialog (yielding to a child's
  `autoFocus` — stealing from a search/rename field would pop the phone keyboard shut);
  Tab/Shift+Tab wrap inside; on close, focus returns to the triggering element. `Confirm`
  additionally gains an Escape-to-cancel handler (capture-phase, so a Confirm stacked above
  an open Sheet closes alone) and lands its initial focus on **Cancel** — the safe default
  under a destructive prompt. `Button` now forwards a `ref` like the other kit primitives.
  `web` 0.72.0→0.73.0.

- **Web: themed toasts replace every native browser dialog** (#488) — every mutation-failure
  path that fired a `window.alert(...)` popup (12 sites: editor tree operations, file-browser
  open/move, board card move, suggestion approve/reject) now raises a themed toast instead — a
  bottom-anchored card in the shell's own style (`role="status"` polite live announcement,
  manual close, auto-dismiss with errors lingering longer than info, identical re-raises
  replacing rather than stacking). The store-driven `Toaster` (`src/stores/toasts.ts`,
  `toast.error()`/`toast.info()`) is callable from any non-hook code path. The editor's three
  `window.confirm` prompts (restore version over unsaved edits, delete file, delete folder) now
  route through the shared `<Confirm>` primitive with the danger treatment. An ESLint
  `no-restricted-syntax` + `no-restricted-globals` guard (the #394 pattern) bans
  `window.alert`/`window.confirm` so native dialogs can't come back. `web` 0.71.1→0.72.0.
- **Mail: thread-aware reply** (#461) — `mail_send` only ever composed fresh messages, so
  the agent's "reply" started a new conversation on both ends: no `In-Reply-To`/`References`
  headers, no Gmail thread association. A new **`mail_reply(message_id, body)`** tool fetches
  the original message's threading headers (a lightweight metadata-only Gmail call — no body
  fetch), then sends with RFC-2822 `In-Reply-To`/`References` (the full reference chain, not
  just the immediate parent) and the Gmail `threadId` in the send payload. The recipient (the
  original sender) and subject (`Re: <original>`, not doubled if already a reply) are derived
  from the original message, so the caller supplies only the new body. Declared a **danger
  action** (ADR-0007) exactly like `mail_send`; `MailProvider` gains the `reply` seam so a
  future non-Gmail provider mirrors it. `mail` 0.7.0→0.8.0.

- **Tasks: create a task list from the UI or the agent** (#474) — previously the only way to
  get a new Google task list was outside epicurus, in Google Tasks' own UI, and the local store
  had no list concept to create at all. A new **`create_list`** provider seam, a
  **`tasks_create_list(title)`** MCP tool, and a board-level **New list** action (shown
  wherever the Add form's list picker already is) all route through `TasksRouter` to the sole
  configured external provider — **Google-only**: the local store is a single implicit list by
  design (ADR-0030), so `LocalTasksProvider.create_list` raises `NotImplementedError` rather
  than a half-working local multi-list system. The returned id is immediately usable as
  `list_id` / `to_list_id` on the other tools, but — like any newly discovered Google list — it
  still needs the operator's one-time enable toggle in the connected-accounts Lists section
  before it appears as a board category; the module has no write path to the operator's
  collection prefs to auto-enable it, a natural scoped follow-up. Renaming/deleting a list is
  deliberately out of scope (destructive; needs a policy for the tasks inside). `tasks`
  0.12.0→0.13.0.

- **NATS authentication** (#50) — the event bus now **requires credentials**; it previously
  ran open, so any client on the internal network could publish/subscribe across all subjects.
  A new `infra/compose/nats-server.conf` defines an account/user model with three roles — `core`
  (full bus), `module` (tenant-scoped subjects), and `sys` (monitoring) — and the `EventBus`
  authenticates with a per-role `NATS_USER`/`NATS_PASSWORD`. The OpenBao bootstrap generates strong
  per-role passwords (recorded in OpenBao, written to `.env.secrets`); compose keeps weak
  `epicurus-dev` defaults so local/dev `up` is unchanged. New modules authenticate as `module`
  automatically via the service template. Enforced **per-tenant** isolation (account-per-tenant)
  is the deferred SaaS-track step (ADR-0066). `epicurus-core` → 0.19.0.

- **OpenTelemetry tracing → Tempo** (#57) — the observability stack's third signal. `epicurus-core`
  gains `epicurus_core.tracing` (`setup_tracing` / `get_tracer`): optional, env-driven distributed
  tracing that instruments FastAPI requests and the NATS `EventBus` (publish / request / handle), with
  W3C trace-context propagated across the bus so one trace spans publisher → handler, exported to Tempo
  over OTLP/HTTP. **Off by default** (`OTEL_TRACES_ENABLED`); a runtime no-op when disabled, so the lean
  stack pays nothing. Spans carry only structure (route, subject, tenant, byte size) — never payloads or
  prompt content, the logs' redaction posture. The service template + echo + core-app wire it, so a new
  module traces out of the box; enable fleet-wide with `OTEL_TRACES_ENABLED=true` and the `observability`
  profile. ADR-0068. `epicurus-core` 0.17.0→0.18.0, `core-app` 0.51.0→0.52.0, `echo` 0.2.2→0.3.0.
- **Discord chat bridge + connect/manage bridges from the web** (#366, #369) — the first real
  Phase-4 bridge, and the operator surface to run it. The `messaging` module now runs **every
  bridge at once** (a `BridgeManager`): the always-on **loopback** echo plus each real bridge,
  dormant until connected — each `messaging.outbound` reply is **dispatched to the bridge named by
  the message**, and a new `POST /bridges/{bridge}/reload` control path lets a bridge connect at
  runtime with no restart. The **Discord** provider (`discord.py`) reads inbound over the gateway
  (DMs always; in a server only when **@mentioned**; ignores its own messages) and posts replies
  over REST (thread-aware, chunked to Discord's 2000-char limit), reading its bot token from
  OpenBao. The core gains a **bridge-admin** surface — `GET /platform/v1/messaging/bridges` plus
  connect (write-only token) / on-off / disconnect — that writes the token to OpenBao and reloads
  the module, so the browser never holds a token (constraint #6). The web adds a **Settings → Chat
  bridges** card (connect/disconnect, an on/off switch, live per-bridge status). ADR-0062.
  `messaging` 0.1.0→0.2.0, `core-app` 0.49.0→0.50.0, `web` 0.65.0→0.66.0.
- **Messaging foundation: chat bridges, inbound → turn → outbound** (#364) — the gating
  foundation for Phase 4. A new **normalized inbox contract** in `epicurus-core`
  (`InboundMessage` / `OutboundMessage` + the `messaging.inbound` / `messaging.outbound`
  subjects + `session_id_for`), the **first inbound NATS consumer in core** — it runs a
  **headless** agent turn per bridge message (keyed `session_id = "<bridge>:<channel>[:<thread>]"`,
  reusing `Agent.run`, persisted like any turn) and routes the reply back out — and a new
  provider-pluggable **`messaging` module** (host port 8093) that carries both ends via a
  `BridgeProvider` seam (`start()` / `send()`), with a built-in **loopback** bridge so the path
  works with no external account and per-tenant bot tokens read from OpenBao
  (`messaging/<bridge>`). Memory/facts stay tenant-scoped → one brain across the web UI and
  every bridge. Power-aware (paused → skip). The individual bridges (Telegram #365, Discord
  #366, …) fan out after this as new providers. ADR-0058. `epicurus-core` 0.15.0→0.16.0,
  `core-app` 0.46.0→0.47.0, new `messaging` 0.1.0.
- **Tasks: drag a card between columns to move it** (#380) — the board could only move a task
  via the move picker / Edit form. Cards are now **draggable**: dropping one on another column
  moves the task, reusing the card's **existing** move action (`tasks_update` with `to_list_id`,
  #257), so the backend contract is unchanged. It applies where a column maps to a list (grouped
  by **list**) — the dragged card's move choices are matched to the drop column by title; dropping
  on a due/status/priority column is a no-op (the move can't change those dimensions). The
  action/Edit path stays as the accessible, pointer-free fallback. `web` 0.56.0→0.57.0.
- **Calendar: choose which calendars are shown, and the month paints instantly** (#378, #379) —
  the calendar view gave no way to hide a busy calendar, and reopening it refetched before
  showing anything. Each event the module returns is now **tagged with its calendar**
  (`calendar_id`, an `account:collection` token), so the view offers a **Calendars** menu of
  per-calendar visibility toggles (each with a colour dot, persisted per page); hiding a calendar
  drops its events client-side with no refetch. And each month window is **cached** in
  localStorage: reopening paints the cached month **instantly** and revalidates in the background
  (stale-while-revalidate, bounded to the last 12 windows). `calendar` 0.9.0→0.10.0,
  `web` 0.56.0→0.57.0.
- **Notes & knowledge: the rendered Preview is now editable (WYSIWYG)** (#377) — the `editor`
  archetype opens render-first, but its Preview was read-only, so editing meant toggling to the
  raw markdown source. Preview is now a **WYSIWYG surface** (Milkdown's Crepe — ProseMirror +
  remark) you type into directly, with **markdown kept authoritative**: edits serialize back to
  the same buffer, so the existing idle/leave auto-save and version history (ADR-0042 / ADR-0046)
  work unchanged. The Edit toggle still drops to the raw source; a **read-only** vault (a watched
  Obsidian mount or the bundled reference docs) still renders without editing. The editor is
  **lazy-loaded** so it never enters the main bundle. Adds the `@milkdown/crepe` dependency.
  `web` 0.56.0→0.57.0.
- **Chat: the assistant can ask a clarifying question mid-turn, answered inline** (#360, ADR-0053)
  — the core `ask_user` tool (backend #345/#361) pauses a turn and ends the stream with an
  `awaiting_input` event carrying the question; until now the web just stopped the spinner. The chat
  now **renders that question with an inline answer input** in the live turn (beneath the partial
  answer), and submitting posts to `POST /platform/v1/agent/runs/{run_id}/resume` so the turn
  **continues streaming** to completion. The pending question is **persisted**, so a hard refresh
  mid-question keeps the prompt (the suspended run stays durable server-side for 24h); the main
  composer remains an escape hatch that abandons the question. `web` 0.56.0→0.57.0.
- **Chat: the Conversations list shows which chats are still generating** (#396) — turns now run
  server-side regardless of the client (#400/#376), so a conversation you've navigated away from can
  still be answering, but the list gave no sign of it. Each session row now shows a subtle **pulsing
  accent dot** while it has an in-flight turn: the current chat reflects its own live state instantly,
  and other sessions are polled (while the list is open) from a new
  `GET /platform/v1/agent/active-runs` — the session ids generating right now (tenant-scoped,
  best-effort: the live-run buffer is a disposable cache). `core-app` 0.44.0→0.45.0, `web` 0.56.0→0.57.0.

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

- **Bound container log growth — logging caps on every compose service** (#462) — no service
  set a Docker `logging:` policy, so every container ran the default `json-file` driver
  **unbounded**; a chatty service, or one stuck in a retry loop, could fill the disk on the
  always-on box. Every service in every compose fragment (the data plane, edge, observability,
  Ollama, SearXNG, every module, and the service template) now sets `driver: json-file` with
  `max-size: "10m", max-file: "3"`. YAML anchors don't cross `include:` boundaries, so a fragment
  defining more than one service (data plane, observability, Ollama) declares its own
  `x-logging` anchor; single-service module fragments inline the block. Verified against the
  merged `docker compose config` (all 24 default-profile services, and all 32 with
  `--profile observability`, resolve the option) and a live `task smoke` run — `docker inspect`
  on running `postgres`/`calendar`/`core-app` containers confirms the driver actually applies at
  the runtime level, not just in the rendered YAML. An operator who wants one override for every
  container regardless of compose edits can instead set `log-opts` in the box's Docker daemon
  config — see [Installation](docs/user/installation.md#container-logs). Infra-only; no
  component version bump.
- **Web: WCAG AA contrast pass on the muted text tokens** (#490) — `--ep-text-faint` measured
  **3.05–3.67:1** in dark and **2.37–2.69:1** in light, below the 4.5:1 AA floor for small
  text, and it is load-bearing at 10px (phone tab labels, the chat "memory on" footer, model
  meta lines). The audit went wider than the ticket and found more: light `--ep-text-dim`
  missed on surface-2 (4.35), light `--ep-gold-strong` (accent badge text / active tab) sat at
  4.27 on its real blended background, dark `--ep-danger` missed on surface-2 (4.12), and the
  light theme reused the dark semantic hexes wholesale (`ok`/`warn`/`danger` error text at
  **1.89–3.63:1** on paper). Every text-role token now clears **≥ 4.5:1 against canvas,
  surface *and* surface-2 in both themes** — dark faint `#6e7064→#8b8d7f`, dark danger
  `#c26d5c→#c97767`, light dim/faint re-tiered `#636555`/`#6b6d5c` (the paper backgrounds span
  a narrow luminance band, so the AA-compliant muted pair is necessarily compressed), light
  gold-strong `#8a6a2c→#795d25`, and new light semantic overrides `#527540`/`#84681d`/`#9d4736`.
  The moon (paused) accent pair already passed and is unchanged. Phone tab labels bump
  10px→11px — primary navigation shouldn't sit at the app's smallest size. A new
  **`contrast.test.ts` gate** parses `index.css` and enforces all of this (plus the
  faint<dim<text hierarchy and the badge worst-case over translucent accent fills), so the
  next theme tweak fails CI instead of shipping an illegible token. Known remaining gap,
  filed separately: the light-theme primary Button label (`text-canvas` on `bg-accent`)
  measures 3.22:1 — a component-level treatment decision. `web` 0.73.0→0.74.0.

- **Knowledge reads the vault through the core file API — its `/data` mount is gone** (#346) —
  the read-path tail of the file-space migration. A new `VaultReader` seam (ADR-0070) puts every
  read site — the incremental indexer, the editor's `read_doc`/`list_docs`, the attachment picker,
  the hover-card resolver, the suggestion-review diff, and the agent read tools — behind one
  interface with two backends: the default **`ApiVaultReader`** speaks `PlatformClient.files_*`
  to the core (so reads follow the swappable local-FS ↔ S3 backend and the module mounts **no**
  `/data` volume — the core is now the **sole** mounter), and **`DiskVaultReader`** serves watch
  mode (#232) and the bundled-docs tree. A core outage **raises and retries** (capped backoff) —
  it can never read as an empty vault and de-index everything; a genuinely absent vault reads
  empty. **Operator note:** Obsidian **watch mode** now needs a `docker-compose.override.yml`
  re-adding the read-only vault mount — see `docs/developer/obsidian-sync.md` for the recipe.
  `knowledge` 0.19.1→0.20.0.

- **Retire the `files-init` one-shot — the core image's entrypoint provisions the tenant
  file-space root** (#421) — after the file-space migration (Phases 2–4) the core is the sole
  writer of `/data` (storage/notes mount nothing, knowledge mounts read-only), and `files-init`
  survived only to `chown` the root-owned `epicurus-files` named volume so the core (uid 10001)
  could write a fresh one. That chown now lives in the **core image's entrypoint** (ADR-0069): a
  small stdlib-only Python entrypoint starts as root, creates and `chown`s **only** `/data/<tenant>`
  (never `-R`, so a bind-mounted Obsidian vault's contents are left untouched), then drops to uid
  10001 and `exec`s the app — which therefore never runs as root. The `files-init` service and the
  `depends_on` from `core-app`/`knowledge` are removed; the module subtrees (`knowledge/`, `notes/`)
  are created by the core on first write (the read-only knowledge indexer already tolerates a
  not-yet-created dir). One fewer data-plane container; completes the #346 file-space arc.
  `core-app` 0.51.0→0.53.0.

- **Shared additive schema reconcile (`epicurus_core.db.ensure_columns`)** (#249) — every store
  evolves its schema with `create_all`, which creates a missing table but never alters an
  existing one, so a column added after a table's first release silently never reached an
  already-provisioned Postgres (the bug that hit `llm_prefs` in #214 and `tasks_local` in #218).
  The per-store `_ensure_columns` helpers — copy-pasted across nine stores — are now one audited
  helper in `epicurus-core` (behind the optional `db` extra; ADR-0067): it adds any model column
  the live table lacks, reproducing the model's type and, where a `server_default` exists, its
  `NOT NULL` + default (so a reconciled column matches a freshly-created one), and relaxes a
  NOT-NULL-without-default column to nullable so the add never fails on a populated table.
  Audited the remaining `create_all` stores (notes, knowledge/notes indexes, core file index, …)
  — all single-release, no drift — and **fixed** knowledge `to_path`'s malformed
  `server_default=""` (which rendered no default at all) to a quoted `''`. No behaviour change
  for existing deployments. `epicurus-core` 0.17.0→0.20.0 (also reconciling its drifted
  `_version.py`, 0.16.0→0.20.0); `tasks` 0.11.0→0.11.1, `calendar`
  0.10.0→0.10.1, `storage` 0.8.0→0.8.1, `knowledge` 0.19.0→0.19.1, `core-app` 0.51.0→0.52.1.
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

- **Files: search no longer strands you at the root; Upload icon-only on phone. Tasks: view
  controls no longer wrap awkwardly** (#619, #620, #634) — three dogfood UX findings on the
  shell's toolbars. **Files search (#619)** is a global, non-path-scoped lookup (server-side),
  so submitting one clears the visible directory — but clearing the search used to leave the
  reader stranded at the root instead of returning to where they were; the client now remembers
  the pre-search directory and restores it. **Files Upload (#620)** always carried the "Upload"
  text label even on a phone, crowding the row alongside breadcrumbs + search; it now collapses
  to icon-only below the `sm` breakpoint via the shell's existing `hidden sm:inline` convention
  (the same one `ActionControl`'s `iconOnlyNarrow` and the calendar toolbar use) — `aria-label` +
  `Tooltip` keep it discoverable, desktop unaffected. **Tasks board toolbar (#634)** rendered
  "Group by"/"Show" as independent flex items sharing a row with the (`ml-auto`-pushed) actions
  cluster, so at common widths the two controls and the actions button(s) wrapped unpredictably
  — a control could end up separated from its sibling, or an action stranded alone on a mostly
  empty second line. The controls now share their own flex cluster, sibling to the actions
  cluster, matching the calendar toolbar's nav-cluster/actions-cluster split — each group wraps
  and reflows as a whole. `web` 0.105.0→0.105.1.

- **Board/calendar actions no longer fail with a raw `NetworkError` when a module is down** (#472) —
  every manifest-declared UI action runs through one dispatch, `McpHost.call`, and it alone among the
  core's outbound module calls had **no timeout and no transport-failure mapping** — unlike the sibling
  `_get_json`/`_post_json` helpers that already turn a slow/dead module into a controlled `502`. So a
  refused or hung module let a raw transport exception escape to nginx as an opaque **502
  NetworkError**, closing the action form with no reason. The dispatch now bounds every hop (connect,
  `initialize`, and the tool RPC) with a 30s timeout and normalizes a transport failure into a new
  `ModuleUnreachableError`, which `ModuleRegistry.invoke` maps to a **502** `… action failed: module
  unreachable` (the agent's tool loop reports it to the model instead of crashing the turn). Finding
  this exposed a latent second bug the mocked `#435` tests hid: the streamable-HTTP client runs its
  transport in an anyio task group, so a `ToolCallError` raised **inside** the `async with` block was
  silently **wrapped in an `ExceptionGroup`** — every `except ToolCallError` missed it. The `isError`
  → `ToolCallError` raise now lives *outside* the transport block, so a tool-reported failure still
  surfaces as a clean **400** with the tool's own message. Verified end-to-end against a **live**
  FastMCP server (the mocks can't reproduce the anyio wrapping): success, `isError`→`ToolCallError`,
  refused→`ModuleUnreachableError`, and a hung socket tripping the read timeout are all now covered.
  `core-app` 0.67.0→0.67.1.
- **Assistant instructions: guard an unsaved draft; cover injection-first on the memory path** (#536) —
  the Settings instructions editor is the first long-form editor there (the other cards instant-save),
  so an unsaved draft silently vanished on an accidental reload/close. It now arms a `beforeunload`
  guard while the draft is dirty and drops it on save (an in-app route change is a declarative-router
  navigation `beforeunload` can't observe — a future data-router `useBlocker` would cover that, noted
  on the issue). Test-side, injection-first (#497) was only asserted on the headless/no-memory path; a
  test now pins the **memory path** order — `[system(instructions), system(recalled), …history…, new
  user]` — and the resume path was confirmed by reading not to re-inject the base prompt
  (`run_stream(resume_convo=…)` uses the rehydrated convo and skips assembly). An explicit
  **empty-prompt escape hatch** (a deliberately blank prompt, distinct from "use the default") is left
  as an owner call — blank still resets to the shipped default; the one-character-prompt workaround
  stands. `web` 0.88.5→0.88.6 (the core-app change is test-only — no version bump).
- **Files: dropping an external file onto a folder row uploads into it instead of vanishing** (#556) —
  a file dropped precisely on a folder row was silently swallowed: the row's drop handler
  unconditionally `preventDefault`ed and ran the internal-move path (a no-op for an OS file drag),
  and the pane-level upload handler then bailed on `defaultPrevented` — no upload, no move, no
  error. The shared directory drop-target now handles an external file drag too: no internal
  drag + the drag carries `Files` → **upload into that directory** (the same "upload lands where
  you dropped" rule as the pane, one level deeper), with the row/breadcrumb highlighting as the
  target and claiming the event so the pane doesn't also upload into the current dir. An in-flight
  internal move-drag still takes precedence, so a reorder is never mistaken for an upload. `web`
  0.88.4→0.88.5.
- **Command palette: `?new=1` no longer silently no-ops on a second trigger, and Enter no
  longer hijacks an IME composition's commit** (#558) — two small gaps found at the palette's
  #544 merge review. `EditorView`'s `?new=1` deep-link (the palette's "New note") used a
  one-way, never-reset latch and never stripped its own param: triggering "New note" a second
  time while already on the notes page (same route, no remount) silently did nothing, and
  `?new=1` survived into the address bar so a reload or bookmark re-opened the create flow. The
  latch now resets when the param disappears (mirroring the `?doc` deep-link's change-detection)
  and the param is stripped once applied via `setSearchParams(…, { replace: true })` in an
  effect. Separately, `CommandPalette`'s Enter handler ran the highlighted entry even when the
  keydown was committing a CJK/IME composition; it now bails on `e.nativeEvent.isComposing`.
  Also, while there: the power action is held back until the `["power"]` query resolves (a very
  fast open-and-click could otherwise send the wrong toggle), the hotkey now excludes `Shift`,
  and the entries `useMemo` depends on the mutation's stable `.mutate` rather than the whole
  `useMutation` result (a fresh object every render). `web` 0.88.3→0.88.4.
- **Calendar (PWA): the toolbar no longer overflows on a phone** (#562) — the calendar page's
  top toolbar packed prev/next chevrons + the month/range label on the left and "New event" +
  the Calendars menu + Today + the view switcher on the right into a row with no shrink or wrap
  escape valve, so at ~380px width the right group clipped past the viewport edge. `ActionControl`
  gains an opt-in `iconOnlyNarrow` capability — the same icon+`aria-label`+`Tooltip` treatment an
  `icon_only` action already gets, but CSS-driven (`hidden sm:inline`) rather than
  module-declared, so it only shrinks the label below the `sm` breakpoint and desktop is
  unaffected; both the calendar toolbar's "New event" and the board toolbar's action opt in. The
  month/range label now carries a short form ("Jul 2026") alongside the full one ("July 2026"),
  CSS-swapped the same way, and the action row keeps a `flex-wrap` fallback for a still-wider
  case (several connected calendars) rather than clipping. `web` 0.88.2→0.88.3.
- **Board/calendar actions: a failed action's error no longer splits the row** (#472) — each
  `ActionControl` rendered its own inline error span as a sibling of its button inside the
  shared `flex flex-wrap` actions row, so a failing action (e.g. Complete on a task card)
  spliced its message between the other buttons (Complete / *error* / Edit / Delete) instead of
  reading as one message under the full row. `ActionControl` now takes an optional `onError`
  callback; a caller laying out several actions in one row (a board card, an event's Edit/Delete
  detail row) lifts the failing action's message into local state and renders it once, below the
  full row, instead of each action rendering its own inline span. A lone toolbar action that
  doesn't pass the callback keeps the original self-contained inline rendering. The raw
  **"NetworkError when attempting to fetch resource"** some task-card actions surfaced is only
  partly closed by this PR — see the issue for the full diagnosis; the remaining piece needs a
  change outside `services/web`. `web` 0.88.1→0.88.2.
- **Saved hosted models: atomic upsert + no junk provider-only rows** (#537) — `POST
  /llm/saved-models`'s `add()` was get-then-insert, so two concurrent first-saves of the same id
  could race in the gap to a composite-PK `IntegrityError` (a 500); it is now a single atomic
  `INSERT … ON CONFLICT DO UPDATE`. And `is_hosted("claude/")` was True — a `/` was present but the
  model part was empty — so a provider-only id persisted a junk `claude/` row; `is_hosted` now
  requires a non-empty model part, so that `POST` is a clean **400**. (Removing a saved id that is
  the current `llm_prefs.global_default` still deliberately leaves the default pointing at it —
  valid for inference, just unlisted.) `core-app` 0.66.2→0.66.3.

- **Files: move/rename can't smuggle a file into a module's subtree** (#554) — `POST /files/move`
  checked neither `src` nor `dst` against the module-owned `locked_prefixes`, though `upload`
  does — and #479 is what made operator files draggable, so the hole was newly reachable: dragging
  a file onto a module folder row (or typing a `/`-bearing rename) landed a foreign file behind the
  module's back, desyncing its index. The move handler now mirrors the upload guard — **400** when
  `dst`'s top-level segment is a module folder and `src`'s differs, so a module's *own* same-top
  move still works — the web rename field rejects a `/` or `\` inline before it can relocate, and a
  pathological name (control char / NUL, or a segment over 255 bytes) is clamped to a clean **400**
  instead of a store-level 500. A scheme-less `module_urls` entry (its host parsed as the URL
  scheme, leaving `hostname` None) now recovers its host so the folder stays locked, warning rather
  than silently unlocking. `core-app` 0.66.1→0.66.2, `web` 0.88.0→0.88.1.

- **Files: a folder present in both the file space and the object store renders once** (#560) — the
  Files page (`GET /platform/v1/files/page`) merges two listing sources — the core file-space tree
  (`store.list_dir` / `index.search`) and the storage module's objects (`objects.list`) — and
  appended them with no dedupe, so a folder (or file) in both trees produced two identical rows. The
  merged listing is now deduped by `(kind, normalized path)`; the file-space source is enumerated
  first and wins a collision, so its movability (#479) stays authoritative rather than an object
  duplicate wrongly forcing `movable=True`. Browse and search both dedupe; sort order is unchanged.
  `core-app` 0.66.0→0.66.1.

- **Chat: expanding a message's Sources pill no longer reveals every hover-card at once** (#572) —
  unnamed Tailwind `group`/`group-hover` pairs compile to a descendant selector that matches **any**
  ancestor carrying `.group`, so a source chip nested inside a message row also reacted to the row's
  hover — expanding "Sources (N)" stacked every card open at once. Both scopes are now named
  (`group/chip`, `group/msg`), following the existing `group/tip` precedent, and the remaining unnamed
  leaf reveals were renamed in the same pass so the trap can't resurface. `web` 0.86.0→0.86.1.

- **Files: de-indexing a folder no longer drops a wildcard-sibling's search rows** (#579) — the core
  file index selected rows to delete with an **unescaped** `LIKE path + "/%"`, and `_`/`%` are SQL
  LIKE wildcards legal in path segments, so de-indexing `data_2024` also matched a sibling
  `data-2024/*` and dropped its index rows (non-destructive — the #390 reconcile watcher re-indexes on
  its next pass — but a transient search/listing gap). A local `_like_prefix()` helper now escapes
  `\`, `%`, `_` with `escape="\\"`, mirroring the storage object-delete fix (#574). `core-app`
  0.64.0→0.64.1.

- **CI: the wiki sync no longer fails red before the wiki's first page exists** (#540) — the
  workflow's `has_wiki` check only confirms the wiki *feature* is on; GitHub doesn't create the
  wiki's own git repo (the `.wiki.git` remote) until a first page is made from the Wiki tab in
  the web UI, so every docs push died with "repository not found" (exit 128) in the meantime.
  A `git ls-remote` probe against that remote now gates the sync the same way the `has_wiki`
  check does — a `::notice::` and a clean skip, not a failed run — until the operator does that
  one-time setup. Infra-only; no component version change.

- **Tasks: overdue-recurrence sweep hardening** (#533, #534, #535, #539) — `tasks_update(due="")`
  on a task with a live repeat rule now rejects instead of silently stranding the series
  (clearing `due=""` and `repeat=""` together still ends it); the sweep and materialization
  compute "today" in the operator's timezone with a UTC fallback (mirroring calendar #433); an
  in-process per-`(tenant, task)` claim stops two concurrent reads double-materializing the
  same anchor and a persistently failing retire from spawning a fresh duplicate on every
  subsequent read; and `tasks_list` text adopts the shared listing cap (the tasks half of
  #539). `tasks` 0.15.0→0.15.1.

- **Mail: 403s no longer conflate rate-limiting with a missing scope; `mail_search` adopts
  `capped_listing`** (#538, #539) — Gmail returns 403 both for a missing OAuth scope and for
  per-user/per-day rate limiting (`usageLimits`); the blanket scope-hint treatment from #513
  misreported the latter as "reconnect Google", so a 403 body's `error.errors[].reason` is now
  inspected first and only a genuine scope reason still gets that hint (an unparseable body
  falls back to it too, since a missing scope remains the more common cause). `mail_reply` also
  makes two Gmail calls under one `try` — a metadata GET (needs `gmail.modify`) then the send
  POST (needs `gmail.send`) — so a 403 on the GET was always reported as the send scope; it's
  now attributed to whichever endpoint actually failed. Also: a whitespace-only `Reply-To`
  header is a non-empty (truthy) string, so it used to "win" over `From` and address an
  unroutable blank recipient — `Reply-To` is now stripped before that check. Separately,
  `mail_search` adopts the shared `epicurus_core.capped_listing` helper (#468/ADR-0084) for its
  listing text instead of hand-rolling it, matching `calendar_list_events`'s adoption
  (`tasks_list` remains hand-built, tracked as the rest of #539). `mail` 0.8.1→0.8.2.

- **Mail: reply/send hardening — Reply-To, scope-hint errors, contract wording** (#513) —
  `mail_reply` now addresses the original message's `Reply-To` header over its `From` when
  both are present (mailing lists, newsletters, and support desks commonly set `Reply-To` to
  route replies away from the sending address); a 403 from Gmail on `mail_send`/`mail_reply`
  (a token missing the `gmail.send` scope) now returns the same reconnect-hint treatment
  `mail_mark_read`/`mail_mark_unread` already have for `gmail.modify`, instead of a bare
  exception; and a self-reply (replying to a message the operator sent themselves) is
  deliberately documented as allowed-by-design rather than left as an unconsidered gap — it's
  indistinguishable from mailing yourself a note, and the danger-action confirm (ADR-0007)
  already shows the recipient before anything sends. `mail` 0.8.0→0.8.1.

- **Calendar: DST-anchored occurrence starts normalize to UTC; attendee carry-over across a
  "following" split now has an explicit test** (#467) — after the ADR-0077 timezone anchor
  (#446), a DST-anchored occurrence's `start`/`end` came back tzinfo-aware **in the series'
  stored zone** (e.g. `2026-11-02T09:00:00-05:00`) instead of the codebase's `+00:00`/`Z`
  convention. Root cause: `_synthesize_instance` builds each occurrence via `model_copy`,
  which — unlike normal construction — never runs `Event._ensure_aware`, so the validator
  alone can't fix it. Two fixes: `_synthesize_instance` (`providers/local.py`) now normalizes
  explicitly, and `_ensure_aware`/`_ensure_aware_optional` (`models.py`) now also normalize any
  aware-but-non-UTC value, closing the same latent gap in Google's `_google_item_to_event`
  (which parses the event's own RFC3339 offset via normal construction). Also adds the
  explicit test that attendees survive a "this and following" split — already correct by
  inspection, just unasserted until now. `calendar` 0.13.0→0.13.1.
- **Knowledge: a direct move/rename no longer strands a stale search hit** (#470) — the
  editor's move endpoint (`POST /pages/{page_id}/move`, drag-and-drop / rename in the UI)
  relocated the file but never told the indexer, unlike the agent-suggestion approval path,
  which already paired a move with a re-index. The old path's ledger row and Qdrant vectors
  lingered indefinitely — showing up as a phantom duplicate in `knowledge_search` — and the
  new path stayed unindexed until the next full re-index. A new `KnowledgeIndexer.move_path()`
  (a single file swaps its vectors directly; a folder move reconciles via a full run) is now
  the one shared implementation both the editor's `move_item()` and the suggestion-approval
  path call, and the move response gains an `indexed` field (mirroring the save endpoint) so
  a failed re-index is visible rather than silent. `reconcile()` also now GCs any ledger row
  whose path the live vault no longer has whenever its Qdrant collection is intact — a cheap,
  no-re-embed safety net that self-heals a stray stale entry on the next startup or retry
  pass, independent of the move fix. `knowledge` 0.20.1→0.21.0.
- **Module tombstone-reconcile and autoconnect warnings no longer log an empty error**
  (#498) — both handlers logged `error=str(exc)` around a bare `except Exception`; for a
  timeout or cancellation (`str(TimeoutError()) == ""`), the warning recorded an empty
  `error` field with nothing to debug from. `reconcile_tombstones()` (a resurrected
  module's re-removal failing) and `autoconnect_collections()` (a module's `/accounts`
  becoming unavailable mid-autoconnect) now log `repr(exc)`, which is never empty — the
  same fix `_probe`'s handler already got in #482. `core-app` 0.57.0→0.57.1.
- **`tasks_update` can no longer silently no-op, and can now clear a due date or notes**
  (#475) — a dogfood session asked the agent to remove a task's due date; it called
  `tasks_update` repeatedly, each call reported success, and nothing ever changed, on the
  task page or in Google Tasks. Three compounding bugs: (1) omitting a field and "clearing"
  it were the same call shape — there was no way to *unset* `due`/`notes`, so the agent's
  only option (omit the field) meant "leave unchanged"; (2) a field-less update was itself
  silently treated as success (Google's provider GETs and returns the current task when
  there's nothing to change, by design); and (3) a mutation with no `list_id` always
  targeted the default list, so a task living in another enabled list 404'd there instead of
  being found — the likely source of the intermittent ✗ failures in the same run. Fixed: an
  empty string (`due=""`, `notes=""`) now explicitly clears the field (Google sends a PATCH
  `null`; the local store writes `NULL` instead of a literal empty string); `tasks_update`
  rejects a call with nothing to change (title/notes/due/priority/tags/status/`to_list_id`
  all omitted) with an actionable error instead of succeeding as a no-op; and
  `complete_task`/`update_task`/`delete_task` now search across the operator's enabled lists
  (the same active → enabled → local order `get_task` already used) when `list_id` is
  omitted, instead of assuming the default write target. `tasks` 0.11.1→0.12.0.
- **Memory recall/save no longer silently break after an embedding-model change** (#436) — the
  `<tenant>__facts` Qdrant collection was created at whatever dimension the embedder had when it
  was first touched; swapping to a differently-sized model left it stale, and every recall/save
  after that 400'd on a vector-dimension mismatch — recall silently degraded to "no memory" and
  new facts silently stopped saving too. `UserFactStore` now checks a collection's dimension
  against the current embedder on first use each process lifetime and **reconciles a drift in
  place**: re-embeds every stored fact's text with the current embedder and recreates the
  collection at the new size, preserving each fact's id and metadata (a fact has no source
  document to cheaply recrawl the way a knowledge doc does, so unlike the module re-embed
  fan-out this never drops data). Also folded into the manual "Re-embed everything" action
  (ADR-0054) via a new **memory facts re-embed** maintenance job, so an operator-triggered
  re-embed refreshes memory the same way it refreshes knowledge/notes. core-app 0.53.0→0.54.0.
- **Facts reconcile no longer drops facts beyond a scan cap** (#450, amends ADR-0074) — the
  #436 dimension-drift reconcile scrolled the collection in a **single, capped pass**
  (`_REBUILD_CAP`, 10,000) and rebuilt the collection from only what that pass returned; any
  fact stored beyond the cap was silently deleted with no source to recover it from, so the
  "never drops data" claim held only below the cap. `UserFactStore._reembed_existing` now
  **pages through the entire collection** following Qdrant's scroll offset until exhausted, so
  every stored fact survives a reconcile regardless of corpus size; the cap now bounds only how
  many points are held in memory per page. core-app 0.54.1→0.54.3.
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
- **The Ollama KV-cache choice now actually applies on a fresh install** — core-app runs as
  uid 10001 and writes `/etc/epicurus/ollama.env` to apply the operator's KV-cache type (#307),
  but the shared `ollama-runtime` named volume is created **root-owned**, so on a fresh stack
  that write failed with `PermissionError`: the choice saved but never took effect, and the
  Ollama container mounts the same volume read-only so it couldn't fix the ownership either. A
  one-shot **`ollama-init`** (in `infra/ollama/compose.yaml`) now `chown`s the volume root to uid
  10001 before Ollama starts (`depends_on: service_completed_successfully`, mirroring
  `qdrant-init` / `files-init`). Ordering-only — the env write is lazy (an operator change long
  after boot), so it never races startup. The runtime-smoke gate asserts `ollama-init` ran and
  exited 0 (#392). Infra-only; no component version change (stack tag set at release).
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

- **Pin the lint/type gates exact — `mypy==2.1.0`, `ruff==0.15.20`** (#514) — the root dev
  group pinned `mypy>=1.13` / `ruff>=0.15.20` with no ceiling, so any `uv lock` re-resolve
  floated the tool upward and the bump rode invisibly inside an unrelated PR's lockfile — a
  green-local/red-CI split (mypy 1.13→2.1.0 flags `session.scalar(select(...))` returned
  directly as `no-any-return`; 1.13 accepts it). Both gates are now pinned to the exact
  version CI already resolves, and the `.pre-commit-config.yaml` ruff hook is bumped to the
  matching `v0.15.20` (from `v0.8.4`, id modernized to `ruff-check`) so `pre-commit` and
  `uv run ruff` are the same binary. Bump them deliberately in their own chore PR. No
  runtime change — dev tooling only.

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
