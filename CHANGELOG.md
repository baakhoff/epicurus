# Changelog

All notable changes to epicurus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`v0.1.0` is the first release — the first version usable on a server with a UI.

A release is cut by pushing a semver tag (`git tag v0.1.0 && git push origin
v0.1.0`); GitHub Actions then publishes the GitHub Release and versioned container
images to GHCR.

## [Unreleased]

- **Calendar now notices changes you didn't make here** (#831) — the three calendar change
  events had exactly one emitter, the provider-write seam, which sees every write made *through*
  this module and, structurally, nothing else: an event created, moved or deleted in Google
  Calendar's own UI was never observed and never announced. Every downstream consumer — the
  automations matcher, push alerts, the event feed — was correct and simply never heard about
  half the changes to the operator's calendar. This adds the second emitter, on the shape mail's
  reconcile proved (ADR-0096): a per-collection incremental sync over Google's `syncToken`, with
  a tenant-scoped local cache of *what this module last observed* — which is what turns a
  provider's flat "here is a changed event" into the three different things the spine wants to
  hear. An id never seen is a creation; an id whose change hash moved is an edit, with
  `time_changed` a real before/after comparison against the cached start/end rather than a
  guess; an id that vanishes is a cancellation with a title still worth printing, since a
  tombstone is little more than an id. An expired token (`410 GONE`) is reported through the
  provider seam as `None` rather than an exception — a lapsed cursor is a recoverable state,
  not a failure — and the loop answers it by full-syncing and **diffing against that cache**,
  so the gap is reported exactly instead of replayed blindly or swallowed; a row that merely
  fell out of the forward-moving window is pruned in silence, because the passage of time is
  not an operator action. A first-ever sync stays silent (a calendar you already had is not
  news), and the signal is the sync-state row's mere existence, so a *restart* resumes and
  reports what changed during the outage rather than absorbing it into a silent prime. The hard
  part is having two emitters without ever double-announcing one action: each write records a
  short-lived durable marker keyed `"<event type>|<provider>:<id>"` — plus the series id when
  the write was series-scoped — which the reconcile consumes (exact id) or peeks (series)
  before emitting. That key deliberately omits the change hash the `dedup_key` carries, so
  suppression survives the provider normalising content on the way back and survives the
  series → occurrence identity shift; both sides now build payloads, dedup keys and suppression
  keys from one module, because two copies of a dedup rule are one copy of a bug. Markers are
  written only for external-provider writes, so a local-only deployment gains no churn at all —
  and by the same one-rule degrade (#815) an unconfigured or disconnected provider resolves to
  zero sync targets, so an idle tick makes no provider call and prints no log line. A recurring
  series arrives as one change per occurrence, so a pass collapses them per
  `(series, event type)` — one click in Google's UI is one event, not thirty — and the whole
  pass is capped, mirroring mail's resume-backlog ceiling. `invitation_received` /
  `attendee_responded` are deliberately still **not** declared: they are Google-only, and a
  declared-but-never-published event would repeat the mistake mail's docs already record as a
  lesson learned once. The manifest's hardcoded version had meanwhile drifted two minors behind
  `pyproject.toml` (0.18.0 vs 0.20.0) — its test pinned the same stale literal, so the gap passed
  the gate while the Modules page badge read it; the manifest is corrected and the test now reads
  the declared version, so it cannot drift again. `calendar` 0.19.0→0.20.0 (MINOR).

- **The document pane now types** (#654) — v1 (#541, ADR-0101) opens the pane when an annotated
  `writes_document` call *lands*, by which point the model has already written the whole body;
  v2 shows it arriving. The blocker was never the pane: tool-call fragments were assembled
  **inside** `LlmGateway.stream_chat` and surfaced only on the final `result`, so the agent loop
  could not observe a call being written at all. So this starts with a gateway streaming-contract
  change — a new optional `StreamEvent.tool_call` carrying `ToolCallFragment{slot, id?, name?,
  arguments?}`, emitted as each fragment arrives. It is strictly additive: the accumulation and
  the final `result` are untouched, and `slot` is the accumulator's *own* slot rather than a
  second guess at it, so #324's hard-won index discipline (OpenAI shares an `index` across a
  call's fragments; LiteLLM leaves it unset for Ollama's complete-per-fragment calls, and
  honouring `index or 0` once fused two calls into invalid JSON that crashed the next turn on
  replay) is inherited rather than duplicated — `arguments` is the fragment's delta, `id`/`name`
  are the call's values as resolved so far, and the whole-dict provider flavour reports no delta
  because it is replaced rather than appended. Reading the body out of that stream needs a value
  extracted from JSON that is still an unterminated fragment, which `json.loads` can only reject:
  `agent/partial_json.py` is a hand-rolled resumable scanner rather than a tolerant-parser
  dependency, because the job is narrower than "parse partial JSON" and its real failure modes
  are the ones a general parser does not solve for us — it hands out the *delta* since last time
  rather than re-decoding the document per fragment, it matches only top-level keys with a real
  scanner (so `{"title": "content", "content": "real"}` cannot confuse the two), and it holds back
  anything a fragment boundary cut in half: a `\` split from what it escapes, a `\uXXXX` split
  from its digits, a surrogate pair split down the middle. That last one is not cosmetic — a lone
  surrogate lives happily in a Python `str` and then fails to encode into the SSE frame carrying
  it, so the pane would have killed its own stream. Malformed JSON marks the reader broken and
  keeps what it decoded; a preview is never worth an exception in a turn. On top sits the throttle,
  the answer to #541's "a large document must never starve the chat deltas": since the document
  and the answer share one stream, what protects the answer is bounding the document, so a
  `doc_preview` frame goes out at most every **100 ms** per call (`PREVIEW_INTERVAL_S`) or sooner
  once **4096** characters have piled up (`PREVIEW_MAX_CHARS`) — the interval bounds the frame
  rate at ~10/s whatever the model's token rate, the cap bounds frame size, the first slice goes
  out immediately so the pane appears at once, and the tracker is flushed when the gateway stream
  ends so the tail is never withheld. The new `doc_preview` SSE kind is the first top-level kind
  added since the protocol settled, and the only **purely ephemeral** one: `{tool, text, preview}`,
  where `text` is the coalesced body delta and `preview` is `{module, target?, title?}`, repeated
  on every frame so a frame stands alone. It never reaches the timeline — live or persisted — for
  the same reason v1's finished `document` payload doesn't (ADR-0041), only more so, because a
  preview reads an *unfinished* call; `target`/`title` are reported only once their argument has
  closed, so the header fills in instead of flickering through a half-typed title. **Re-attach
  needed a decision and got the smaller one:** previews ride the same seq-tagged live-run buffer
  as every other frame, so `GET /runs/{id}/stream?after_seq=N` replays them in order — from 0 a
  reload rebuilds the body from its deltas, from N a resume gets only what it missed. Holding the
  latest coalesced snapshot and re-sending *that* would make the run carry document state it has
  no other reason to keep, and would hand a resuming client a prefix it already has. On the web
  the pane opens on the first character the model types and steers on every one after it: the
  store concatenates deltas into `liveDocument` (`streaming: true`), a caret marks text still
  arriving, and the panel's `replace(payload, title)` finally has its first caller (#659's
  affordance) so a title that finishes typing late reaches the header instead of leaving it
  reading "Document" for the whole write. The hand-off is v1's settle path unchanged and is what
  keeps ADR-0101's promise that the pane never lies about whether a write happened: the following
  `tool` frame carries the arguments as actually *parsed* and replaces the previewed body
  wholesale, so a preview that drifted cannot outlive the real thing. The pane stays **read-only**
  from the first previewed character through the settle, so #541's edit/write conflict — the one
  v1 sidestepped structurally — stays impossible rather than becoming live; closing the pane
  mid-typewriter stays closed for the rest of that write. `core-app` 0.113.0→0.114.0 (MINOR),
  `web` 0.136.0→0.137.0 (MINOR).

- **Send it any link and the agent can actually read it** (#739) — `web_search` could *find* a
  page; nothing in the platform could *open* one. No URL-fetch tool existed anywhere, in
  websearch or in the agent's built-ins, so "save the substance of this article to my knowledge
  base" bottomed out at a search snippet plus whatever the model happened to remember about the
  site. The save half was already done (`knowledge_propose_edit`, #220/#722) and so was gateway
  vision (#633/#711) — but only for a chat attachment, never for an image behind a URL. The
  missing piece was the reading. websearch gains a second tool, **`link_ingest(url)`**,
  returning `{kind, title, site, author?, published?, text, image_descriptions?, transcript,
  notes}` across three tiers: articles and ordinary pages (readability-grade body text plus
  OpenGraph/JSON-LD metadata), direct images (described by the core's vision model), and public
  video/reel/audio links (metadata plus the uploader's own subtitles). It stays **inside
  websearch** rather than becoming a module of its own — the roadmap forbids new modules before
  1.0, and metadata-only ingestion needs no ffmpeg and no OS packages, so the image is
  unchanged. It calls **no other module** (ADR-0004): the tool returns an extract, and composing
  it into a document and filing it is the agent's own next step through the knowledge tools —
  which is why the steering lives in the tool docstring and in a new rung on #703's grounding
  ladder in the default agent instructions (read the link, work from what came back, keep the
  source URL and retrieval date on anything filed). Extraction is `trafilatura`, chosen over
  `readability-lxml` + `beautifulsoup4` because one dependency covers both the body text and the
  metadata the contract needs; `yt-dlp` is a lazily-imported, failure-tolerant extra that
  degrades to oEmbed + OpenGraph when it is absent or an extractor breaks overnight.

  Two things are deliberately refused rather than approximated. **Transcription**: `transcript`
  is reserved and always empty — ASR is a new gateway modality (milestone 5.0.0) — and the
  platform's own machine captions are not used either, since passing a platform's speech
  recognition off as the video's text would blur exactly the line the contract draws;
  uploader-published subtitles *are* included, in `text`, labelled as captions.
  **Authentication**: no sign-in, no credentials, no cookie jar, no CAPTCHA circumvention —
  structurally rather than as a promise, since the extractor is handed no credential source at
  all and a URL carrying `user:pass@` is refused outright. A private or login-walled link comes
  back as `kind: "unreachable"` with a note saying so, and the same is true of a dead host, a
  PDF, a captionless video, or an image no model could see: every failure is a well-formed
  result carrying an honest note, never a raised exception. The agent's job is to relay the
  gaps, not to fill them.

  This is the first tool in the platform that fetches an **arbitrary, operator-supplied URL from
  inside the Docker network**, where the core, Postgres, Valkey, Qdrant, OpenBao, and the docker
  proxy all answer without authentication — and nothing server-side existed to reuse. So it
  ships a purpose-built SSRF guard: `http(s)` only; no credentials in the URL; single-label
  hosts refused (on this network a dotless name *is* a service name) along with `.localhost`,
  `.local`, `.internal`, `.localdomain`, `.home.arpa`, and `.onion`; the host resolved and
  **every** returned address checked against the private, loopback, link-local, reserved,
  multicast, and CGNAT ranges in both families, with IPv4-mapped and 6to4-wrapped IPv6 unwrapped
  first so `::ffff:127.0.0.1` cannot smuggle a loopback through; and — the part that matters
  most — redirects followed **by hand** so every hop is re-validated before it is requested,
  because a public URL that 302s to `169.254.169.254` is the classic exploit and the hop is what
  it turns on. Bytes, wall-clock across all hops, redirect count, and content types are all
  capped, tunable through new `LINK_INGEST_*` settings with conservative defaults. yt-dlp does
  its own HTTP outside that client, so it is confined to an allow-list of known public media
  platforms and only ever sees a URL that already passed the guard. One residual gap is stated
  rather than papered over: the guard resolves the host and httpx resolves it again to connect,
  so DNS rebinding between the two is not caught — closing it needs connect-time address
  pinning, which httpx does not expose without a custom transport.

  Tier 2 forced a gap in the core into the open. `POST /platform/v1/chat` had **no vision
  gate**: `supports_vision` guarded only the interactive agent turn (#633), so a module sending
  image content-parts to a text-only model got either a silent ignore or a raw provider error —
  precisely the two outcomes that gate exists to prevent — and `link_ingest` would have been the
  first module-initiated vision inference to hit it. The endpoint now applies the same check
  before any provider call and refuses with a structured **400**
  (`{"error": "unsupported_media", "message": …, "model": …}`) a caller can branch on; both the
  OpenAI `image_url` and the Anthropic-native `image` spelling trigger it, and an image anywhere
  in the history counts, not only in the last message. websearch catches exactly that 400 and
  degrades to metadata plus a note naming the model and the fix, so an operator with no
  vision-capable model configured still gets the article — just without the picture described.
  Text-only requests are untouched and pay no extra capability lookup. No new client helper was
  added to `epicurus_core`: `ChatMessage.content` has accepted `str | list[dict] | None` since
  #633, so the module builds content parts over the existing `PlatformClient.chat` and the wire
  contract is unchanged. `websearch` 0.2.2→0.3.0 (MINOR), `core-app` 0.112.0→0.113.0 (MINOR).

- **Going Google-free is now a first-class path, per module and platform-wide** (#764) — the
  operator's instinct was that it wasn't possible at all, and the shell had been quietly proving
  them right. Everything needed already existed: the ADR-0030 collections panel could untick a
  Google list, and Settings could disconnect the account outright. But "stop using Google in
  tasks" was N individual unticks, after which the Google block kept its full visual weight and
  nothing anywhere said the module was now local-only — configuration archaeology dressed as a
  setting. The panel now offers **"Stop using Google in this module"**: one write against the
  existing prefs API that disables every one of that account's collections, clears the write
  target back to the built-in local default (and only when the active collection belonged to
  that account — a second provider's target is never collateral), and collapses the block to a
  single quiet row, *"Google — not used · Use again"*. Tokens are untouched, so it is per module
  by construction: every other module keeps working, nothing at Google changes, and one click
  undoes it. Two deliberate decisions, recorded as ADR-0122. The collapsed state is **derived,
  never stored** — "not used" *is* "none of this account's collections are enabled", the only
  place it could live given `CollectionPrefs` holds `{enabled, active}` and nothing else, so it
  can never disagree with the toggles it replaces (safe as a default because connecting an
  account seeds every collection enabled, #209, making a connected account with nothing ticked
  always a deliberate act). And **"Use again" re-seeds rather than replays**: it enables all of
  the account's collections and makes the first writable one active — exactly what a fresh
  connect does — because restoring a hand-picked subset would need hidden session state that
  silently stops working after a reload, i.e. two behaviours behind one button; the toggles show
  what is on, so re-narrowing is one tick.

  The other half was the global disconnect, which worked but didn't *look* like it had.
  Disconnecting deletes the tokens and strips the provider from every module's stored selection
  (#209), yet the web kept several deliberately long-lived caches describing what modules can
  see — the calendar's account view holds Google calendar names and colours for five minutes,
  the mailbox its thread list for thirty — so the next page the operator opened still painted a
  connected account. The disconnect mutation now invalidates the module-facing keys by prefix
  alongside its own status. And mail, the one module with **no local provider** (ADR-0032: no
  collections, no fallback mailbox), had no story at all for the disconnected state: the page
  relayed a raw `httpx.HTTPStatusError` under a scope hint that didn't apply, and the tools
  re-raised it at the agent. The token seam now distinguishes the case —
  `GmailProvider._get_token` maps the core's documented 404/400 to `MailNotConnected`, translated
  *there* precisely so it can never be confused with Gmail's own "no such message" 404 — and
  every surface answers honestly: each MCP tool returns one model-actionable sentence naming both
  ways out (connect it in Settings, or disable the module) instead of raising, `mail_send`
  refuses to compose a draft that could never be delivered, the page returns a valid empty list
  carrying `disconnected` that the shell renders as an honest empty state with a tap to each
  exit, a leftover message chip answers 503 rather than a 404 claiming the message was deleted,
  and `mail.sync_failed` is no longer emitted — an absence the operator chose is not a failure to
  alert on. The local cache is kept but not served, so a reconnect restores the mailbox with no
  resync and no restart. Not in scope, and deliberately so: local-first replacements for the
  Google-backed capabilities themselves (no local mailbox, no richer local calendar) — going
  Google-free means keeping the local half, not re-implementing the other one. New operator note,
  `docs/user/running-without-google.md`. `web` 0.135.0→0.136.0 (MINOR), `mail` 0.18.1→0.19.0
  (MINOR).

- **Folded the Can into the Tasks page as a Show → Backlog option, instead of a second nav
  entry** (#820) — the Can's own **partition** (#766, the board shows only dated tasks, the
  backlog holds the rest) was right; its **placement** wasn't. A second left-nav page gave
  the backlog the visual weight of its own module and split one workflow — triage the
  backlog, schedule things onto the board — across two pages. The *Show* control (already
  shared by the board and the Can, open/completed/all) gains a fourth value, **`backlog`**:
  a page-level dated-ness partition, deliberately **not** a widened `TaskScope` — the app
  branches on it before the provider fetch rather than teaching the providers a fourth read
  scope. The Can's own `PageSpec` and its `GET /pages/can` route are gone (`can` now 404s
  like any other unknown page id); its data now comes back from
  `GET /pages/board?show=backlog`, rendered by a new `build_tasks_backlog` that keeps the
  Can's exact shape — a flat column, an Add with no due/repeat field, and each card's
  leading Schedule action. *Group by* is omitted whenever Show is Backlog (a flat backlog
  has nothing to group, the same dead-knob rule #767 already gives List/Calendar), and the
  **Calendar** view drops `backlog` from Show's own options entirely — a backlog has no due
  dates to place on a grid — correcting a stale or explicit `show=backlog` back to Open
  whenever `view=calendar`, so the control's echoed value is always inside its own offered
  options. One axis nuance needed a decision: Show used to mean *status scope* on the board
  and, independently, the Can page's *own* Show filter over the backlog; folded onto a
  single control, only one Show value can be active at a time, so the backlog can no longer
  carry that second, independent filter. The chosen rule fetches every status for the
  backlog regardless and splits internally — open and in-progress tasks lead in a flat
  **Backlog** column, any completed undated task follows in its own muted **Completed**
  column (an ordinary struck-through card) — rather than making completed/all leak undated
  items onto the dated board, which would have broken its "dated tasks only, no 'No date'
  bucket" invariant. Either column is dropped when empty, neither is ever omitted, which is
  what keeps the acceptance bar: no undated task, open or completed, becomes unreachable.
  Agent- and web-form-facing copy was swept from "the Can" to "the backlog" / "Show →
  Backlog" throughout — `tasks_add`'s tool description, both `due` parameter descriptions,
  and a task's hover-card `href` (now a `?show=backlog` deep link on the one Tasks route
  rather than a separate page's URL) — so the words the agent uses match what the operator
  sees.
  `tasks` 0.22.1→0.23.0 (MINOR).

- **Disabling a connected calendar account silently emptied the Calendar page** (#814) —
  `CollectionRouter` resolved a *missing provider* in exactly opposite ways on the write and
  read paths. The trigger is a stale `enabled`/`active` collection reference: nothing prunes
  the operator's stored selection when the account behind a reference is disconnected, so the
  dead reference stays in place. A write (`calendar_create_event`, `calendar_find_free`) hit
  `self._provider_for(ref.account) or self._local` and fell back to the local store; the
  `list_events` aggregate hit the same condition and `continue`d — and because its fallback
  (`prefs.enabled or [_LOCAL_REF]`) only triggers when `enabled` is *empty*, never when a
  reference inside it is dead, it never consulted local at all. So an event created through a
  stale reference was written, persisted, and then absent from the Calendar page, while the
  tool honestly reported a real, saved `Event`. Neither side was lying; they simply disagreed
  about what "this reference's provider is gone" means. The skip was also completely silent —
  the neighbouring transient-failure path logs, but a disconnected account left no trace, so
  nothing in the logs hinted that a read source was being dropped. This is the calendar twin
  of the same defect reported against tasks (#795); the blast radius was smaller here only
  because `_search_refs` always appends the local reference, so single-event lookups
  (`get_event`/`update_event`/`delete_event`) already walked past a dead reference and reached
  local — the aggregate read had no such backstop.

  Fixed by giving every read and write one shared rule, `_resolve_provider`: a live provider
  for the reference's account is used as-is; a missing one **degrades to the local
  collection** and logs a warning naming the account and collection that went missing. The
  degraded reference is replaced by the local reference rather than merely re-pointed, so a
  write is never handed the dead account's collection id and the events a degraded read
  returns are tagged `local` — a token the page's per-calendar visibility toggles can match,
  rather than a calendar the operator no longer has. A `_dedup_refs` helper applies the rule
  across a multi-reference read and de-duplicates by *effective* target, so two independently
  stale references — or a stale one landing on a local entry already enabled — read local once
  instead of double-counting its events in the returned window. Transient read failures keep
  their existing skip-and-log (#209): a provider that is live but erroring is a different
  condition from one that is gone. With no `active` set the write-default scan is choosing
  among candidates rather than honouring a stated one, so passing over a dead reference there
  stays silent and unchanged; an explicitly-set `active` that has gone stale degrades to
  local, deliberately not to some other external calendar the operator never named.
  `calendar` 0.18.1→0.19.0 (MINOR).

- **The MCP SDK moved to 2.0** (#792) — a genuine breaking API change in the two places that
  carry the module↔core contract: `epicurus_core.module`, imported by every module, and the
  core's MCP host. `FastMCP` became `MCPServer` (the decorator API itself unchanged), every
  transport parameter moved off the server constructor onto `streamable_http_app()`, the result
  attributes went snake_case (`inputSchema`→`input_schema`, `isError`→`is_error`, the wire JSON
  unchanged via aliases), `call_tool` returns a `CallToolResult` rather than a
  `(content, structured)` pair, the low-level transport/session/`initialize()` layering
  collapsed into a single `Client` owning all three with `float`-seconds timeouts instead of
  `timedelta`s, and the SDK now raises `httpx2` exceptions from its own httpx fork. **No module
  source changed**: the `EpicurusModule` constructor, the `tool()` decorator, `manifest()`,
  `http_app()`, and the `session_manager.run()` lifespan hook all survive 2.0 verbatim. Two
  additive members close the one hole 2.0 opened — module *tests* were reaching through
  `module.mcp` into raw SDK surface that moved — so `EpicurusModule.call_tool()` is now the
  stable in-process invocation surface returning the pre-2.0 pair, and `ToolError` is
  re-exported from `epicurus_core`; an SDK reshape now lands in the library once instead of in
  every module's tests. `McpHost`'s outward contract is byte-identical: discovery returns the
  same specs and routes, and a call still distinguishes a tool that ran and reported failure
  (#435) from a module that never answered (#472), with every hop bounded. One deliberate
  hardening: discovery now carries the same 30s RPC read bound as a call, because 2.0's default
  HTTP read timeout is 300s — sized for long-lived SSE streams, far too generous for a scan
  that runs on every agent turn.
  `epicurus-core` 0.33.0→0.34.0 (MINOR), `core-app` 0.112.0 (MINOR), and a test-only PATCH for
  every module whose tests were migrated: `tasks` 0.22.1, `mail` 0.18.1, `calendar` 0.18.1,
  `notes` 0.12.1, `knowledge` 0.27.3, `storage` 0.9.1, `echo` 0.5.1, `websearch` 0.2.2.

- **A push notification could die at any of six gates without a single trace** (#797) — push
  crossed six independent conditions on the way to a device (no event subscription, the
  per-subscription rate cap, the category's push toggle, quiet hours, the tenant-wide rate cap,
  no registered device) and several of them declined in complete silence, so "I got no
  notification" was indistinguishable from a broken pipeline and there was no way to bisect the
  two without instrumenting the code. Every gate now logs one distinct, greppable line naming
  the tenant and the category or module/type — `event alert declined: no subscription for this
  event`, `event alert rate cap reached`, `push skipped: push disabled for this notification`,
  `push queued for quiet-hours digest`, `push rate cap reached; delivery skipped`, `push
  skipped: no registered devices`, `push send failed`, `pruned dead push subscription` — and a
  clean send logs none of them, so a decline line always means a decline. Settings gained a
  delivery-state surface behind a new `GET /platform/v1/push/status`: the registered-device
  count and the most recent delivery *attempt* with what became of it, in plain language, where
  a send that reached zero devices with failures reads as failed rather than as success. A
  warning banner now fires at configure time when push is enabled somewhere but no device is
  registered — the misconfiguration that used to fail silently once per delivery. The
  last-attempt readout is in-memory, single-instance v1, the same disposable-cache trade the
  rate windows make: `null` after a restart means "nothing attempted since boot", not "never".

  **Behavior change — the notification center is now a superset of push, amending ADR-0102 §4
  and ADR-0104 §1.** `push` and `center` used to be independent per-category toggles, so a push
  missed while the device was off — or that failed outright — could simply be *gone*, and
  `center: off` suppressed the durable row entirely. The center row is now written
  unconditionally for every notification that fires, quiet-hours-queued and outright-failed
  deliveries included, and `push` means "*also* deliver to devices". `ChannelPrefs.center` is
  vestigial: still accepted, stored, and returned on the wire, so nothing on the contract
  breaks, but delivery no longer consults it, and `NotifyResult.notification_id` is therefore
  always set. **Anyone relying on `center: off` to suppress rows is affected** — that state no
  longer exists; the only way for an alert not to be recorded is for it not to fire. The event
  alerts card's independent Push/Center switch pair collapses accordingly into two coupled
  switches over the unchanged `{push, center}` wire shape — **Alert** (the master, on iff either
  stored flag is on) and **Push** (also deliver to devices, implying Alert) — and legacy
  `{push: true, center: false}` rows read as both on. The per-subscription event-alert rate cap
  still gates the whole notification, center row included: a firehose into the center is still a
  firehose. The tenant-wide push cap gates delivery only.
  `core-app` 0.111.0 (MINOR), `web` 0.134.1→0.135.0 (MINOR).

- **Tasks added while an account was disconnected were saved and then invisible forever**
  (#795) — `TasksRouter` resolved a *missing provider* in exactly opposite ways on its write and
  read paths, and the trigger is a stale `enabled`/`active` collection reference, which nothing
  prunes when the account behind it goes away. A write fell back to the local store — but kept
  the stale reference, so the task was mislabeled with the account and collection it had fallen
  back *from*, visible in the emitted `tasks.task_created` event's `dedup_key` and in the list
  id handed to the local provider. The read aggregate hit the very same condition and
  `continue`d past it, never trying local at all, because its fallback only fires when `enabled`
  is *empty*, never when an entry inside it is dead. So `tasks_add` reported a genuine,
  persisted task and the very next `tasks_list` or board read came back empty — not lost, just
  unreadable through the router — and the skip was silent, unlike the neighbouring read-failure
  path which logs. Fixed by giving every read and write one shared rule, `_resolve_provider`: a
  live provider for the reference's account is used as-is; a missing one degrades to the local
  collection — replacing the reference rather than merely re-pointing it, so a write is never
  mislabeled — and logs a warning naming the account and collection that went missing. A
  `_dedup_refs` helper applies the rule across a multi-collection read and de-duplicates by
  *effective* target, so two independently stale references, or a degraded one landing on a
  local entry already enabled, read local once instead of double-counting its tasks. The
  explicit-`list_id` write branch funnels through the same rule, which also closes a related
  asymmetry: a `list_id` matching a stale enabled reference now degrades to local instead of
  falling through to "the sole external provider owns this unlisted id" and potentially
  misrouting to an unrelated account. Pruning the stale reference itself is deliberately left
  alone — the core's disconnect cleanup is best-effort by design and a module-side prune would
  need a new write-back into the shared library — because the router-level rule makes staleness
  permanently harmless whatever caused it. The calendar module had the identical defect; it is
  fixed separately as #814.
  `tasks` 0.21.0→0.22.0 (MINOR).

- **`mail.received` only fired while the Mail page was open, so automations and push alerts on
  new mail never ran** (#796) — the event had exactly one emitter, `CachedMailbox.reconcile`, and
  exactly one caller: the mailbox page's `?reconcile=1` read, which the web fires when an operator
  opens Mail. Mail had no background worker at all, unlike calendar and tasks, both of which have
  shipped a scheduler since #664. So "notify me when mail arrives" could only ever fire *after* the
  operator had already seen the mail — the inverse of the feature. Nothing downstream was broken:
  the automation matcher and the per-event alert listener were wired and waiting on an event nobody
  emitted. Worse for the reported case, a cold or expired cursor fell back to a full sync, which by
  design emits nothing (the no-firehose rule), so the first open after a while — exactly when the
  most mail had accumulated — was the one guaranteed to announce nothing at all.

  Mail now runs the loop itself (`epicurus_mail.poller`, started and cancelled by the service
  lifespan, the same shape calendar and tasks use). A tick is `is_available()` →
  `reconcile("INBOX")` and nothing else: deliberately only a *caller* of the existing path, so the
  events, payloads, dedup keys and every consumer downstream are literally the ones the page
  produces, and the two can never drift. It is **on by default** at `MAIL_POLL_INTERVAL_S=300`
  (`0` disables the loop entirely) — an event contract that holds only while a human is watching
  the page is not a contract, and the cost is one history-delta call per tick. An unconnected
  deployment costs one token-presence check per tick and logs nothing at all.

  Two supporting changes fall out of having a second caller. Reconcile is now **single-flight per
  account**, so a poll tick and a page read serialize on the change cursor instead of both
  announcing one message (per process, like the tasks scheduler's `_claim_materialize`; the
  spine's message-id `dedup_key` is the backstop beyond that). And the no-firehose rule is split
  in two by the cache's `synced_at` stamp: a **first-ever** sync still emits nothing, while a sync
  that *resumes* a mailbox synced before — the cursor lapsed while the service was down — replays
  `mail.received` for what arrived since that instant, newest first, capped at 50 messages, via a
  new capability-gated `MailProvider.messages_since` (Gmail: one `messages.list` with an
  epoch-second `after:` term). A provider that can't answer inherits the empty default and simply
  gets no replay; the full sync itself is never blocked or failed by it.
  `mail` 0.17.0→0.18.0 (MINOR).

- **Entities the assistant named in an answer were plain text, never clickable** (#794) — the
  shell has rendered an inline `epicurus://entity/<module>/<kind>/<ref_id>` markdown link as an
  interactive chip since ADR-0019, excluding anything so linked from the trailing "Sources"
  pill, but the model was never told the syntax exists — so it never emitted one, and every
  reference fell through to the pill unconditionally. The one place the model does see
  reference ids, the "Referenced items" block appended to a tool result whose envelope carries
  entity refs (ADR-0079), framed them as tool *inputs* only: "pass an item's id to a tool that
  needs one". That block now carries each reference's ready-made link beside its id, and the
  intro teaches the syntax — link what you mention, using the URL shown, verbatim. Fixing it in
  that one place fixes every module at once, the same reasoning that introduced the id listing.
  Every component of the URL is percent-encoded, `/` included, because the web's inline-link
  matcher stops at the first `)` or whitespace and then decodes: a `ref_id` containing either
  would otherwise break the link, and being markdown, break it *silently*. Handing the model
  the finished URL rather than three raw fields to assemble removes the failure mode entirely.
  The instruction is deliberately to link what is actually named rather than everything — a
  twenty-hit search should not become twenty chips, and the pill remains the outlet for the
  long tail. No web change: the renderer, the pill exclusion, and the graceful degradation for
  an unknown or malformed link all already existed.
  `core-app` 0.109.0→0.110.0 (MINOR).

- **A bare weekday name landed the event exactly one week late** (#793) — the `now` built-in
  handed the model the current date, time and weekday *name*, leaving weekday-to-date
  conversion as head arithmetic: count forward from today's weekday, carry the month or year
  boundary, hope. Nothing anywhere downstream could catch a slip, because the calendar accepts
  any well-formed ISO date — a date a week out is a perfectly valid event, not an error — so
  the failure was silent and self-confident, and "Wednesday" reliably meant the Wednesday after
  the one that was asked for. `now` resolves it in the tool now rather than describing it: the
  payload gains `today` and `tomorrow` as ISO dates, plus an `upcoming` map from every weekday
  name to its next date, strictly in the future — asked on a Friday, `upcoming.Friday` is next
  Friday, not today. All three are computed from the same zone-resolved instant the rest of the
  payload already uses, so the operator's configured timezone decides what "today" is rather
  than UTC, the same trap #559 closed for calendar read paths. The tool description now tells
  the model to take `upcoming` verbatim and never to count days itself, and to ask rather than
  guess when phrasing like "next Monday" is genuinely ambiguous *and* the difference matters.
  `core-app` 0.108.0→0.109.0 (MINOR).

- **Cold-switching documents in the editor could save the outgoing note's content over the
  newly-opened one — data loss** (#781) — every `EditorView` host (Notes, Knowledge, the chat
  document pane) shares the same Preview surface: Milkdown's Crepe (#377), deliberately
  *uncontrolled* after mount — it reads its markdown once and, by design, ignores later prop
  updates, so a live cursor never fights a reset; the parent instead re-keys the component on
  the document path to reseed it with fresh content, per its own doc comment. That contract
  broke on a **cold** switch — opening a document whose content hasn't reached the browser yet.
  `openDoc` flushed and re-pointed `selectedPath` to the new document immediately, remounting
  the WYSIWYG under its new key, but the seed that hands it real content only runs once the
  fetch resolves and the placeholder-data guard (#712) lets it through — so the remount picked
  up whatever the buffer still held: the *outgoing* document's markdown. Crepe mounted on it
  while `selectedPath`/`seededPath` had already both moved on to the new document, and the next
  transaction in that live-but-foreign surface — a click, a keystroke, Crepe's own post-init
  normalization — reported it back through `onChange` as if it were a genuine edit of the new
  document. Every save guard then passed honestly: the idle timer, the re-click flush, and the
  leave/refresh flush each wrote the *old* note's content to the *new* note's path. A warm
  switch (the document already cached this session) never showed it — the seed lands during the
  same render, before the remount commits — which is why "click over and back" looked fine and
  made the bug easy to miss in practice.

  Fixed with two independent layers. The editor pane now gates on `selectedPath ===
  seededPath`: neither the WYSIWYG nor the raw-source view mounts until the newly-selected
  document's own content has actually seeded the buffer — a cold switch shows a brief loading
  state instead of a frozen, still-editable snapshot of the old note; a warm switch stays
  instant, since the seed already resolves before the gate is checked. As defense in depth, the
  WYSIWYG now reports the identity of the document it was mounted for (`docKey`) with every
  change, captured once at mount and never re-read; the parent drops any report whose `docKey`
  no longer names the live `seededPath`, so a stray write can never land on the wrong document
  however it arises. The vitest mock for the WYSIWYG was itself part of why this stayed
  invisible to the suite: it was fully *controlled* (`value={value}`, re-rendering on every prop
  change), so it could never reproduce a stale mount — it is now `defaultValue`-based and
  mount-faithful, like the real Crepe. Every save already snapshots a version (ADR-0046); a note
  clobbered by this bug can be recovered from the previous entry in its History dropdown.
  `web` 0.134.0→0.134.1 (PATCH).

- **The "can't reach epicurus" banner no longer flaps on a single dropped request**
  (#791) — the banner (`ConnectionBanner`, `src/App.tsx`) used to be one single-strike
  fetch failure away from covering the shell: any one `TypeError` or gateway 502/504,
  anywhere in the app, rendered it on the next paint, and any one healthy response
  cleared it — on a loaded self-hosted box (CPU-only inference, Docker Desktop VM) a
  transient gateway hiccup reads exactly like an outage, so the banner appeared
  regularly, lived a few seconds, and vanished, with nothing shown about what actually
  failed. The banner's own signal (`confirmedDown`, `src/stores/connection.ts`) is now
  debounced: a first failure only arms a `pendingDown` state, and `useConnectionWatch`
  immediately fires one confirming re-probe (the same vitals refetch recovery already
  uses); the banner renders only once that evidence persists past a short grace window
  (`UNREACHABLE_GRACE_MS`, 5 s) or a second failure confirms it sooner — so an ordinary
  blip resolves silently and a genuinely stopped stack still surfaces within ~5 s.
  `coreDown` itself is unchanged (still eager, single-strike) — the composer's Send-gate
  and the Files/Mail "connection lost" states still refuse instantly rather than wait out
  a debounce window on an action that would just fail anyway. Every report now also
  carries its evidence — the failing request's method, path, and failure class
  (`TypeError`/`502`/`504`) — kept in the store and logged via `console.debug`, and named
  in the banner's `title` tooltip on hover, so a flap can be attributed without
  network-tab archaeology. `web` 0.133.0→0.134.0 (MINOR).

- **The automations `push` sink is wired up — all ten starter templates now deliver
  something** (#723) — `push` has been valid sink vocabulary since the automations engine
  shipped (ADR-0105), but `SinkDispatcher` never had a handler registered for it: a run
  configured with `sinks=["push"]` executed, recorded a ledger row, and delivered nothing an
  operator could see — exactly the case every one of the ten starter templates #717 shipped
  was in. `make_push_sink` closes the gap the same way `document_sinks.py` closed it for
  `notes`/`kb`: a thin adapter that calls `PushService.notify` under a fixed `"automation"`
  category (with `automation_id`, so a per-automation override can silence just one without
  touching the category default), title the automation's own name, body the run's raw output.
  It goes *through* `notify()`, never around it, so quiet hours, the tenant-wide rate cap, and
  the push/center toggles all apply exactly as they do for the settings UI's test button or
  #732's event alerts. `PushService.notify` gains `NotifyResult.notification_id` — the
  notification-center row's id, set the moment that row is written regardless of what push
  delivery itself then does — so the sink can record it as an `EntityRef`
  (`module="core"`, `kind="notification"`) on the run's `artifacts`, the same ledger field the
  notes/kb sinks already populate; the runs feed renders a chip for it with no further UI
  work. `PushService._send_now` also gains a 500-character cap on the outgoing push payload's
  body (an automation's full report should not be able to blow a push service's own size
  ceiling) — the notification-center row keeps the caller's complete text regardless. `core-app`
  0.107.0→0.108.0 (MINOR).

- **Nightly reflection can no longer propose edits to the base system prompt** (#762) — the
  pass (ADR-0093) offered two targets: a named playbook, or `"instructions"` — the base
  prompt itself. The second target is removed, by design: reflection reads **tainted
  transcripts** (mail bodies, web results, document text the agent quoted), so outside text
  could surface as a plausible edit to the very document the agent's rules live in; an
  instructions proposal was a **full-document replacement** reviewed nightly under approval
  fatigue, and a poisoned approval would persist in every future turn's system prompt; and
  the governed system drafting its own governing document is a conflict of interest even
  with review. Enforced at three layers: the reflection prompt now offers **playbooks only**
  (the base prompt stays in its context read-only, explicitly non-proposable, so playbooks
  don't duplicate base rules); `_resolve` drops any `"instructions"` target the model
  returns anyway (logged); and the review sink refuses to stage a proposal against the
  instructions path **for every origin**, with the instructions apply-path removed outright
  — no future proposal source can quietly reintroduce the target. A pending instructions
  proposal from before the upgrade still renders with its diff and is cleanly rejectable;
  Approve refuses it with a clear message. Operator editing of the base prompt in Settings
  is unchanged. Reflection also gains its **own off-switch**,
  `PLAYBOOK_REFLECTION_ENABLED` (default on, now that it is playbook-only): disabled, the
  nightly job reports "skipped — disabled" without spending a gateway call — previously the
  only way to stop it was disabling all nightly maintenance. `core-app` 0.106.0→0.107.0
  (MINOR).

- **Invisible chats** (#772) — a ghost toggle left of the model chooser starts a **fresh
  invisible session**: everything works normally while you're in it, and when you leave —
  toggling off, switching sessions, starting a new chat, or closing the app — the
  conversation is **deleted, not archived** (the #771 cascade, so nothing survives anywhere).
  The design is *persist-flagged, then hard-delete*: the session writes normally under a
  server-side flag (`PUT /agent/sessions/{id}/ephemeral`, table `ephemeral_sessions`), so an
  accidental mid-chat reload keeps the thread, while the flag hides it from the sessions
  list and from **every learner** while live — never enqueued for fact extraction (checked
  at learn time in both extraction modes, failing closed), excluded from nightly
  reflection's transcript scan (via an `include_ephemeral=False` default on the store's
  `sessions()`), and from `memory_search`'s past-conversation half. An **orphan sweep** —
  at startup and on every session-list read (`GET /agent/sessions?active=<live one>`) —
  erases any flagged session a crash left stranded, sparing the client-named live one and
  any session with a turn in flight. Honest semantics, in the UI's own words: the header
  reads "Invisible — deleted when you leave", and the footnote states that nothing is
  learned while tool effects (tasks, mail, files) persist — like downloads from a private
  browser window; usage metering still records tokens (the events carry no content).
  Normal chats are byte-identically unaffected. `core-app` 0.105.0→0.106.0 (MINOR),
  `web` 0.132.1→0.133.0 (MINOR).

- **Deleting a chat now deletes the chat — everything it produced, everywhere** (#771) —
  `DELETE /agent/sessions/{id}` removed only the `agent_messages` rows, so a "deleted"
  conversation lived on across every sidecar it had touched. Worst: the fact-extraction queue
  (ADR-0051) carried the exchange *text* with no `session_id`, so a deleted chat's queued
  exchanges could not even be targeted — the nightly drain still distilled the "deleted"
  conversation into memory facts that night. Also surviving: the uploaded attachments' bytes
  (`agent_attachments`), the per-session model override (`session_models`, #707), and any
  paused runs (`agent_suspended_runs` / `agent_pending_drafts` / `agent_pending_approvals`) —
  each carrying the conversation verbatim — while an in-flight turn kept generating into the
  deleted session. Now: the queue is **stamped** with a nullable `session_id` at enqueue
  (reconciled additively, ADR-0067; legacy rows drain as before), and the route runs a
  tenant-scoped **cascade** (`SessionDeleteCascade`) — cancel + evict the live run, purge
  queued extractions, clear the model override, drop the paused runs and the automation badge
  mapping, delete the attachment bytes, then the messages last (a failure partway leaves the
  session visible and retriable). The audited referencing stores keep deliberate exceptions:
  `scheduled_turns` rows are operator-authored standing config (kept — the next delivery
  starts the session afresh), `notifications` carry no session linkage (kept), and facts
  distilled on *previous* nights are curated memory managed in the Memory view — the web
  confirm dialog now states that boundary honestly instead of implying total erasure.
  `core-app` 0.104.0→0.105.0 (MINOR), `web` 0.132.0→0.132.1 (PATCH — confirm-dialog copy).

- **Tags become a first-class part of the tasks board: group by them, and pick them as
  chips** (#763) — the model and tools carried `tags` all along, but the UX was half-wired:
  a bare comma-separated text input and no grouping. **Group by → Tags** joins the board's
  dimensions, offered (like *List*) only when a visible task actually has a tag — the first
  **multi-membership** grouping: a task appears under **each** of its tags, untagged tasks
  land in an **Untagged** column, and columns sort alphabetically (case-insensitive,
  Untagged last, stable across reloads). The add/edit forms' tags field becomes a **chips
  input** — "a box inside a box": removable chips inside the field, Enter/comma commits,
  backspace erases into the chips — via a new `format: "tags"` hint (the ADR-0082 seam),
  with a typeahead over the module's distinct tags supplied as **`field_suggestions`** (a
  new open-valued `field_choices` sibling: suggestions are offered, never enforced, so a
  new tag is created by typing it). Serialization stays the tool's comma-separated string —
  the MCP contract is unchanged. **Google honesty**: tags are local-only (Google Tasks has
  no such field; the provider ignores them on write), so the field is hidden wherever the
  write would land on Google — a Google task's Edit form, and Add forms whenever the
  (external-only) list picker shows — never a silent no-op; Google tasks group under
  Untagged. Drag-and-drop stays honest too: list-grouped columns now carry their
  **`list_id`** and the shell matches drop targets by id — never by a display title a tag
  column might share — and with no list columns on screen, cards don't offer a grab that
  could only dead-end (drag-to-retag deliberately not implemented in v1). `tasks`
  0.20.0→0.21.0 (MINOR) · `web` 0.131.0→0.132.0 (MINOR).

- **The Tasks page gets three representations of the same data — Board, List, and
  Calendar — switchable in place** (#767). The `board` archetype gains a **reserved `view`
  control** (an extension of ADR-0049's declarative controls): a board page declaring
  `{id: "view"}` opts into the shell's client-side representations, rendered via the
  standard segmented view switcher. **List** flattens the columns to one deduped row set —
  title, due, priority, list, tag chips, the same per-card actions — with every column
  client-side sortable (stable, missing values last); **Calendar** is a month grid
  (Monday-start, the calendar archetype's geometry) placing each card on its due date,
  with prev/today/next paging, chips toned by the board's overdue/today language, and a
  chip opening the ordinary card + actions in a sheet (no new mutation paths anywhere).
  To support this, board cards additionally carry their **structured fields** — `due`
  (bare ISO date), `priority`, `tags`, `list_title` — as data beside the rendered badges
  (additive; a board that sets none renders as before). The chosen view **persists per
  page** (localStorage, the #743 pattern), a `?view=` deep-link wins over the stored
  choice (and is kept in sync on switch), junk clamps to the kanban, and a board with no
  `view` control ignores the param. The tasks module declares the control (Board / List /
  Calendar), echoes the clamped `?view=`, and omits *Group by* off the Board view —
  grouping shapes kanban columns; the flat/date-keyed views would render it as a dead
  knob — while *Show* applies under every view; the payload is identical across views.
  Undated tasks never appear in any of the three — they live in the Can (#766). The
  calendar *page's* cross-module task overlay (#469) is untouched. `BoardView` is now
  keyed per page in `ModulePageScreen`, so tasks' two board pages can't leak control
  state onto each other. Day-cell quick-add is deferred. `tasks` 0.19.0→0.20.0 (MINOR) ·
  `web` 0.130.0→0.131.0 (MINOR).

- **The Can: undated tasks get their own backlog page, and the board shows only what's
  scheduled** (#766) — undated tasks used to land on the Tasks board in a "No date" bucket
  under the due grouping and inside every other grouping's columns, so the backlog and the
  plan shared one surface. The tasks module now declares a second left-nav `board` page,
  **Can** (`/m/tasks/can`): one flat **Backlog** column holding every task without a due
  date, across the same enabled lists the board aggregates (category badges preserved). The
  board and the Can partition the same fetch by `due` alone — the board excludes undated
  tasks under **every** grouping (the "No date" bucket is gone). Each Can card leads with a
  one-tap **Schedule** action (a due-only `tasks_update` form prefilled to today, rendered
  as the shell's native date picker via a new `format: "date"` hint on both tools' `due`
  params — the ADR-0082 format seam, no new web code); clearing a due date moves a task
  back to the Can. The Can's Add offers no due or repeat field, and its only view control
  is *Show*, so completed backlog items stay reachable. Nothing vanishes silently:
  `tasks_add`'s description and both `due` param descriptions (which double as the web
  form's field hints) say an undated task files into the Can, and a task's hover-card
  `href` now targets the page it actually lives on. Purely a read-partition — no provider
  or DB change; lead-time notifications are untouched (they key on due dates, which Can
  items don't have — by design). `tasks` 0.18.0→0.19.0 (MINOR); no web change — the shell
  already renders manifest pages, form actions, and the date field.

- **Approving a delete suggestion no longer reports success when the file is already gone**
  (#761) — `SuggestionReview.approve`'s `delete` branch caught the file API's 404 (the vault
  document had already been removed some other way) and swallowed it outright, so both
  callers landed on ordinary success: the operator's approval in the Suggestions UI closed
  silently, and review-off auto-apply printed "Delete of '<path>' applied directly — review
  is off" even though nothing was deleted. The suggestion is still fully resolved either way
  — index cleanup runs, the audit row is recorded, the row drops from the queue, exactly as a
  real delete — a stale suggestion must not be left un-resolvable just because there's nothing
  left to remove. But `approve` now raises afterward instead of returning a false "approved":
  a 404 naming the path and suggesting a next step (the #742 not-found bar: `"<path>" does not
  exist — it may have been deleted or moved; list the folder for current contents`), so the
  Suggestions UI shows an error instead of a silent close, and the review-off tool call fails
  structurally instead of claiming the delete happened. `knowledge` 0.27.1→0.27.2 (PATCH).

- **A knowledge indexer test no longer fails one run in 256** (#769) — `test_indexer`'s
  `test_mtime_ns_reconciles_from_a_raw_seeded_value` opened by asserting that a raw
  `st_mtime_ns` and the indexer's derived `round(mtime * 1_000_000_000)` must differ, i.e. that
  the `int → float → int` round-trip is always lossy. It is not: a float64's ULP near `1.8e18`
  (the epoch in nanoseconds, as of 2026) is exactly 256, so any mtime landing on a multiple of
  256 round-trips unchanged and the two agree — about 1 run in 256, and only where stamps carry
  nanosecond granularity (ext4/CI; NTFS's 100ns ticks hide it entirely). Because `quality` is a
  required check, an unlucky run blocked unrelated PRs — it was hit twice during the 2026-07-29
  train, once on an npm-only change, which is what identified it as ambient rather than caused.
  The scenario needs a ledger row whose `mtime_ns` *disagrees* with the derived value; how the
  disagreement arose is immaterial, so the fixture now supplies one directly instead of relying
  on float imprecision. Every real assertion in the test is untouched. `knowledge` 0.27.0→0.27.1
  (PATCH).

- **The docs link checker now validates anchors in source references, not just the file** (#725)
  — tier 2 of `scripts/check_docs_links.py` scans shipped source for repo-relative `docs/….md`
  strings and asserted only that the file exists, so a comment pointing at
  `docs/foo.md#some-section` kept passing after the heading it named was renamed or removed —
  exactly the rot the checker exists to catch, and it fails silently: the reader follows the link
  and lands at the top of the page with nothing reported anywhere. Anchored references now
  resolve against the target file's headings, the same way tier 1 already validates internal doc
  links. `_anchors_in()` additionally recognizes explicit `{#custom-anchor}` markdown syntax
  alongside the auto-generated slug, so a heading that pins its own anchor is accepted rather
  than reported broken. Tooling only — no package version bump (ADR-0017 scopes SemVer to
  `services/*` and `libs/epicurus-core`).

- **Vision models are no longer mistagged as code models in the model catalog** (#727) —
  `_derive_tags` matched the bare substring `cod(?:e|er|ing)` against each catalog entry's name
  and description, and "encoder" contains "code": llava, whose blurb describes a *vision
  encoder*, came back tagged `code`, so the Models page filed it under coding models and the
  `code` filter offered a model that cannot write code. The pattern is now word-bounded. Its
  sibling `multiling` keeps a **leading** boundary only, deliberately — it is a prefix that must
  still match "multilingual"/"multilinguality", and bounding both ends silently dropped
  gemma3:1b's tag, which `test_parse_derives_tags` caught on the first push. Ships inside
  `core-app` 0.104.0.

- **First boot pulls the default models instead of 404ing until someone finds the Models page**
  (#773, ADR-0118) — models are never baked into the Ollama image (the core owns the model
  lifecycle, constraint #8, ADR-0010), so a fresh install booted an empty volume and the first
  chat or embedding call failed with `model not found`, while the knowledge indexer failed
  noisily in the background — verified on a from-scratch deployment. The core now closes that gap
  itself: a fire-and-forget lifespan task waits for the runtime, resolves the deployment's
  *effective* chat + embedding defaults (stored prefs, else `LLM_DEFAULT_MODEL` /
  `MEMORY_EMBED_MODEL`), skips hosted-prefixed ids, and pulls whichever are missing through the
  same `gateway.pull()` the Models page uses — then applies the same post-pull context sizing
  (#386), so a first-boot model opens correctly sized too. It never blocks startup, readiness, or
  a live turn; per-model retries back off exponentially (Ollama resumes partial downloads) and
  exhaustion is a warning naming the Models page, never a crash. An unreachable runtime — a
  hosted-only deployment running no Ollama at all — costs one warning after a bounded wait.
  `LLM_BOOTSTRAP_MODELS` tunes it: `auto` (default), blank to disable, or an explicit
  comma-separated list; the CI smoke override pins it blank, since the gate asserts the wiring,
  not the weights. `core-app` 0.103.0→0.104.0 (MINOR).

- **Observability no longer mounts the raw Docker socket** (#724, #726, ADR-0109) — Prometheus
  and Alloy each bind-mounted `/var/run/docker.sock` for container service discovery and log
  tailing, and Prometheus additionally ran as `user: root` purely to read it — full Docker API
  access, write included, for two components that only ever read. Both now reach Docker through
  `docker-proxy-observability`, a third read-only `wollomatic/socket-proxy` sibling of
  `docker-proxy-core` and `docker-proxy-traefik`, gated behind the same `observability` compose
  profile so it exists only when that stack is up; Prometheus's `user: root` override is gone
  along with the mount. The allowlist is GET/HEAD-only and was derived from a live boot rather
  than copied from `docker-proxy-traefik` — both consumers use Prometheus's `discovery/moby`
  library, which issues a `HEAD /_ping` probe Traefik never sends and calls `GET /networks` to
  resolve each container's network labels, and omitting either makes discovery error outright
  rather than merely degrade a label; Alloy's `loki.source.docker` separately needs
  `GET /containers/{id}/logs` to tail at all, whose absence fails silently. New
  `tests/test_docker_proxy_allowlist.py` renders the real `docker compose config` and pins all
  three proxies' allowlists by equality, so widening one fails as loudly as breaking one — it
  also asserts the socket reaches nothing but the proxies (scanned across every rendered service,
  not a named few) and that the hardening flags (`cap_drop: [ALL]`, `read_only`,
  `no-new-privileges`) stay put. Infra, docs, and a repo-root test only — no service or library
  version bump.

### Added

- **Inbox category tabs on the mail page** (#765) — Gmail's Primary / Promotions / Social /
  Updates tabs, in our mailbox: a strip over the Inbox list, each tab carrying its unread count
  and a dim one-line preview of its newest message, and Forums appearing only when it has mail
  (exactly as Gmail hides an empty one). Selecting a tab filters the thread list through the same
  label/query mechanism the rail and search already use, so **Primary correctly means
  inbox-minus-categorized**. Gmail's `CATEGORY_*` labels stay out of the folder rail where
  ADR-0087 put them — a tab over one folder is a different UI element, and this is that element.
  The `MailProvider` seam gains two **concrete, capability-gated** members — `list_categories`
  (the tabs, as presentation-ready data) and `category_query` (the single point where a neutral
  tab id becomes provider query syntax) — so a provider that doesn't classify mail inherits the
  no-op defaults and the page renders exactly as it did before. The strip is drawn entirely by
  the shell from module *data* (ADR-0018): no markup leaves the module, and the neutral tab ids
  are the local-first seam — a future local mail provider can back the same tabs by classifying
  through the core's LLM gateway (constraint #8) with zero shell change. Assembling the tabs is a
  provider fan-out, so they are cached in a new tenant-scoped `mail_category` table on a short TTL
  (`MAIL_CATEGORY_TTL_S`, default 60s, with a negative-cache row so an *uncategorized* mailbox
  isn't the expensive case); a mark-read drops that cache, so the active tab's count converges
  through the shell's existing invalidation with no full reload. `mail_search` gains a matching
  `category` argument, so "summarize today's Promotions" works in chat without the model knowing
  Gmail query syntax. `mail` 0.16.0→0.17.0 (MINOR) · `web` 0.129.0→0.130.0 (MINOR).

- **External file mounts** (#731) — add a directory to Files the way you'd add a drive: an
  operator binds a host folder (or a whole drive) into `core-app` via a compose overlay
  (`services/core-app/compose.external-mounts.yaml`, `task external-mounts-up` — never a
  default bind, never auto-loaded), and it shows up as an additional top-level root on the
  Files page and in the agent's `storage_list`/`storage_search`/`storage_read` tools,
  addressed as `mount:<name>/<sub-path>`. Read-only by default; a declared `rw` mount is
  writable through the same operations the tenant space has (upload/write/delete/move/mkdir)
  — enforced **server-side** (403), not just hidden in the UI. Traversal and symlink-escape
  attempts are rejected (400) via `PathEscapeError`, a new `epicurus_core.files` exception
  type an app-level handler catches uniformly. Indexing/watching is **opt-in per mount**
  (`FILES_EXTERNAL_MOUNTS_INDEXED`, with exclude globs) — a whole-drive mount never
  auto-indexes by default. `LocalFileStore` gained a `tenant_subdir` flag so a mount can
  address its own root directly rather than nesting a `<tenant>/` segment inside the
  operator's drive. A move never crosses between the tenant space and a mount, or between two
  mounts — mounts are isolated compartments. `epicurus-core` 0.32.0→0.33.0 (MINOR) ·
  `core-app` 0.102.0→0.103.0 (MINOR) · `web` 0.128.0→0.129.0 (MINOR).

- **Per-event alerts** (#732) — "push me when X happens" for any module-declared event, no
  automation required. A tenant-scoped `(module, event_type) -> ChannelPrefs` subscription
  store, off by default, backs a new `EventAlertListener` wired beside the automations engine
  at the same event-intake seam — a dumb fan-out (no agent turn, no ledger, no autonomy dial),
  not an automation. `PushService.notify_effective` delivers through the existing
  center/quiet-hours/rate-cap send path for a caller whose channel prefs don't come from a
  category; a second, per-subscription rate cap keeps one chatty event type from spending a
  tenant's whole push budget. An automation triggered by the same event still fires
  independently — two notifications, by design. Settings gains an "Event alerts" block,
  grouped by module with a Custom section for a free-typed `(module, event_type)` pair.
  `core-app` 0.96.0→0.97.0 (MINOR) · `web` 0.125.1→0.126.0 (MINOR). ADR-0114.

- **Persisted maintenance-run history** (#733) — the Maintenance orchestrator's `last_run` was
  a single in-memory slot, gone on restart, with no record of whether a run was scheduled or
  manually triggered. A new tenant-scoped `maintenance_runs` table is the durable counterpart,
  written via an `on_recorded` hook (mirroring `AutomationRunner`'s live-runs-feed pattern) on
  every completion — including a shutdown-interrupted batch, persisted from `shutdown()` itself
  since the driver's own `except CancelledError` can't safely await again once caught. Every run
  now carries a `source` (`scheduled` | `manual`). `GET /platform/v1/maintenance`'s `last_run`
  reads the store (survives a restart); a new paginated `GET /platform/v1/maintenance/runs`
  pages back through it, newest-first; retention prunes past a row cap or age cutoff as its own
  nightly-eligible maintenance job. Settings' Maintenance card gains a "Run history" list —
  time, source, duration, per-job status chips, expandable detail, "Load more" pagination.
  `core-app` 0.97.0→0.98.0 (MINOR) · `web` 0.126.0→0.127.0 (MINOR). ADR-0116 (amends ADR-0060).

- **`ask_approval` — pause a turn for an inline Approve/Reject of a staged change** (#745) — a
  third sibling of `ask_user` (ADR-0053) and draft-first send (ADR-0085): after the model stages
  a change through an existing propose tool, it can call `ask_approval(summary, refs)` to pause
  in place rather than telling the user to go check Suggestions separately. The chat UI renders
  an inline card (summary + a chip per staged entity); Approve/Reject each call the linked
  change's owning module's *own* review-decision endpoint directly — the operator's own click,
  never the agent, preserving `suggestions.py`'s existing "the agent must never approve its own
  work" boundary — then resume the turn with the outcome. Chat-only by construction: the tool is
  spliced only into the interactive streaming path, which has no `automation_id` concept at all,
  so it is structurally absent from every automation/scheduled/inbound-bridge turn regardless of
  autonomy level — those keep today's async review-queue behavior unchanged, no new toggle
  needed. A new `agent_pending_approvals` table (a third sibling of
  `agent_suspended_runs`/`agent_pending_drafts`) holds the pause; it decays to the same async
  queue on expiry (`ASK_APPROVAL_TTL_HOURS`, default 24h) — nothing lost either way.
  `core-app` 0.100.0→0.101.0 (MINOR) · `web` 0.127.0→0.128.0 (MINOR). ADR-0117 (extends ADR-0053,
  ADR-0033, ADR-0085).

- **Knowledge and Notes remember where you left off** (#743) — opening the page always
  landed on the module's default project, with every folder collapsed back to its initial
  state and no document open, no matter where you'd actually been working: the active
  scope, fold state, and open document were all plain component state, hydrated from
  nothing on every mount. All three now persist to `localStorage` — the scope choice keyed
  by `(module, pageId)`, fold state and selection by `(module, pageId, scope)` since tree
  paths are scope-relative (restoring one project's folds onto another would be worse than
  restoring nothing). Reopening the page — navigating away and back, switching projects and
  back within one visit, or a full reload — restores scope, then folds, then the open
  document, in that order; the document lands in preview per #729, same as any other open.
  An explicit `?doc=` deep-link or a host-provided document (#541) still always wins over
  restored state — they set the selection during render, before the restore effect (which
  only ever fills an untouched slot) gets a chance to run. Decay is graceful: a restored
  scope that no longer exists falls back to the module's own default (which re-triggers the
  same restore for whatever that default resolves to); a restored document that's gone is
  simply never selected, so it never gets a doc-fetch to 404 on — no error flash, just the
  ordinary empty-state prompt. A real bug surfaced while testing this against a genuine
  remount (not just a scope switch within one mount): the write-back effect was gated only
  on `scope` being known, not on the restore having actually run for it yet, so on a fresh
  mount it fired one render early with the plain in-memory defaults and clobbered the very
  state the restore was about to read — fixed by gating the write on the same "restored for
  this scope" marker the restore effect itself sets. Scroll position and in-document
  read-position are out of scope. `web` 0.124.0→0.125.0 (MINOR).


- **Move Knowledge documents by drag-and-drop, and an honest actions menu** (#741) — there
  was no way to move a document to another folder (or back to the root) at all, and the
  row's "three dots" — the icon that universally means "more actions" — just deleted the
  file; the real rename affordance was a bare `ChevronRight`, easy to mistake for
  navigation. The move machinery already existed (`moveItem`, which rename already used) —
  the gap was purely presentational. File rows are now **drag-and-drop movable**: drop one
  on a folder row to move it in, or on the tree's own background to move it to the top
  level; a collapsed folder auto-expands the moment a drag hovers over it, and Escape (or
  dropping outside any target) cancels for free — the browser fires `dragend` regardless of
  how the drag ended. The three old hover icons are replaced by one honest **⋯ actions
  menu**: Rename, Move to… (a compact folder picker), New file in folder (folders only),
  and Delete — still gated by the same themed confirm (#488), no longer the bare ⋯ icon's
  own click action. Moving the currently open document keeps it open at its new path (no
  dead pane, no 404 fetch) — the existing `moveItem.onSuccess` already handled this for
  rename, unchanged. Both Move to… and drag-and-drop reuse the not-yet-saved guard #740
  added for rename: moving an unsaved document swaps its path locally instead of a server
  call that would 404. Drag-and-drop is desktop-only by design (native HTML5 DnD doesn't
  fire from touch input), so the menu is what covers mobile and touch. Folder-to-folder
  moves are out of scope for this pass — files only. Notes is untouched: `can_manage_files`
  stays false there, so it shows no hover actions, same as always. `web` 0.123.1→0.124.0
  (MINOR).



- **Resizable tree panel for Knowledge and Notes** (#730) — the editor archetype's document
  list fixed the tree column at 18rem, so long titles truncated on a narrow tree and a wide
  screen couldn't give the tree more room. A drag handle now sits between the tree and the
  editor: pointer-drag resizes live (clamped to 12rem–40rem), double-click resets to the
  18rem default, and the handle is keyboard-accessible (`role="separator"`, arrow keys nudge
  the width) rather than mouse-only. The width persists per `(module, pageId)` in
  `localStorage` (`editor-tree-width:<module>/<pageId>`) — the same pattern CalendarView
  already uses for its own view state — so Knowledge and Notes remember independent
  preferences. Below the `sm` breakpoint the layout is unchanged (a single stacked pane, so
  there's nothing to divide); the fixed-width grid column becomes a CSS custom property
  (`--tree-w`) read only by the `sm:` grid-template, so the responsive stack is undisturbed
  by the same inline style always being present. One archetype change covers both pages
  (Notes and Knowledge share the `editor` archetype, ADR-0018) — a shared `ResizableSplit`
  component is deliberately not extracted yet; that's for if/when a second consumer (e.g. the
  Files browser) actually needs one. `web` 0.122.1→0.123.0 (MINOR).


- **Knowledge/notes: suggestion lifecycle tools** (#744) — the agent could only *add* to its
  review queue; "change that suggestion" had no better option than proposing a near-duplicate,
  and the agent had no way to see what was already pending. Four new tools per module —
  `{knowledge,notes}_list_suggestions` (the pending queue: id, kind, target, a content
  preview), `_read_suggestion` (one suggestion's full proposed content), `_update_suggestion`
  (revise content/note/target in place — stays pending, `proposed-at` refreshes, one queue
  entry never two), `_withdraw_suggestion` (retract a pending suggestion, kept as history) —
  let it check first and revise instead of duplicating. Update/withdraw stay strictly inside
  the pending queue and never touch the vault/note, so they're safe to expose even though
  approve/reject correctly stay off the MCP surface (letting the agent approve its own
  proposals would defeat the review gate, ADR-0033). A refusal on an already-resolved or
  unknown suggestion raises with a message naming *why* (already approved/rejected/withdrawn,
  or unknown outright) rather than a bare 404 the model can't act on (#697 precedent). Notes'
  `notes_read_suggestion` is a deliberate, narrow exception to "notes are private, no read
  tool" — it echoes back only the agent's own draft, never a note's stored body. Every
  existing propose-shaped tool in both modules now nudges the agent to check the pending
  queue first. `knowledge` 0.26.0→0.27.0 (MINOR) · `notes` 0.11.0→0.12.0 (MINOR).
- **Per-saved-model capability overrides** (#711) — a saved hosted model can now carry the
  operator's correction to what the core *believes* it can do. LiteLLM's static cost map is the
  only source for a hosted model's vision support and context length, and it is missing ids
  entirely (`grok/grok-latest`, which resolves to an unmapped `xai/grok-latest`) and mislabels
  others; the consequence was concrete — `supports_vision()` resolved `False` and the image gate
  (#633) refused image turns for a genuinely vision-capable model, leaving "rename your model to
  a mapped id" as the workaround. The override is consulted **before** the map in both
  `supports_vision()` and `_hosted_details`, and applies even when the map lookup fails outright
  — an unmapped id is precisely the case it exists for, so that path must not be the one that
  skips it. `vision: auto` (the default) with no `context_length` is today's behaviour exactly,
  so an absent override changes nothing. New `PUT /platform/v1/llm/saved-models/capabilities`
  (404 for an unsaved id — an override is a property of a saved row, never a way to create one);
  `GET /saved-models` rows now carry both the **resolved** capabilities and the raw `override`,
  so the editor round-trips without a client-side merge. The Models page's hosted-model sheet
  gains the controls, and the saved rows finally render capability badges (they never did) —
  driven by the resolved answer, so an override shows the moment it's saved. Two new columns on
  `saved_models`, reconciled additively (ADR-0067). Gating and display only: routing, keys, and
  metering are untouched (constraint #8). The `get_model_info`/`supports_vision` lookup misses
  now warn **once per model id per process** and debug thereafter — an operator-saved alias
  outside a curated static map is expected, not anomalous. `core-app` 0.92.1→0.93.0 (MINOR) ·
  `web` 0.119.0→0.120.0 (MINOR).

### Fixed

- **The OpenBao app token no longer silently expires 32 days after bootstrap** (#728) — the
  bootstrap minted the token with `-explicit-max-ttl=0` and called it "non-expiring". That flag
  only removes the *explicit cap*: the result was a plain service token falling back to the
  system default lease TTL of 768h, and nothing ever renewed it. Every self-hosted deployment
  therefore lost **all** secret reads exactly one month after bootstrap — provider keys, OAuth
  clients and tokens, module config, bridge credentials, push VAPID keys — presenting as a
  per-path `permission denied` that reads like a policy bug (and, after a restart, as
  `OpenBao client is not authenticated`, with core-app refusing to start). No test shape catches
  this: CI's vault is minutes old, so the defense has to be design. The bootstrap now mints a
  **periodic orphan** token (`-orphan -period=768h`) — the only non-root token with an unlimited
  lifetime — and `SecretStore` grows the maintenance to match: `renew_self()`, a
  `run_token_renewal()` loop core-app starts from its lifespan (daily against a 768h period, so
  renewal can fail for weeks before it becomes an outage), and a **403 self-heal** that drops
  the cached client, re-authenticates, and retries the operation once. Because the token is
  re-resolved on rebuild, a rotated `OPENBAO_TOKEN_FILE` now takes effect without a process
  bounce. Renewal failures are bounded warnings (the first, then every 7th) with a recovery
  line, never a crash; a non-renewable dev root token is detected with a lookup-self and the
  loop exits quietly. A 403 that survives the retry now names token expiry as the likely cause
  instead of reporting a bare "permission denied". `messaging` holds the same token, so the
  core's single renewer covers it too. **Existing deployments must still mint a periodic
  replacement by hand** — this prevents future expiry, it cannot revive an already-expired
  token; the migration is in [docs/infrastructure/secrets.md](docs/infrastructure/secrets.md),
  and the symptom now has a runbook entry in `startup-and-recovery.md`. Also repairs a drift the
  release process missed: `epicurus_core.__version__` still read `0.30.0` while the package
  published `0.31.0` — a test now pins the two together. `epicurus-core` 0.31.0→0.32.0 (MINOR) ·
  `core-app` 0.101.0→0.102.0 (MINOR).

- **The agent now verifies a stale-memory entity before mutating it, and recovers instead of
  blind-retrying a not-found** (#742) — reported from a real trace: create a file via chat,
  delete it in the Files UI, come back to the same chat and ask to move it — the agent acted on
  the path it remembered from earlier in the conversation and failed on a nonexistent file.
  `DEFAULT_AGENT_INSTRUCTIONS`'s "Doing things" paragraph gains two rules: **verify before
  mutating** — something known only from earlier turns, not a tool result just now, gets
  re-checked (list/stat/read/search) before a mutating call relies on it; **recover on
  not-found** — when a tool reports something missing or different from expected, re-ground
  with a list/search and report reality rather than retrying the same call. Both are prompt
  text, so an operator who has already replaced the default doesn't inherit them automatically
  — a docs note covers porting them into a custom prompt. Alongside: `files_routes.py`'s
  `read`/`stat`/`move` 404s now name the path and suggest a recovery step (`"<path>" does not
  exist — it may have been deleted or moved; list the folder for current contents`) instead of
  a bare `"not found"` — the platform file API's own contract, hardened independently of which
  specific tool a caller used. `core-app` 0.99.0→0.100.0 (MINOR — a behavior-visible
  instructions change, the #704 precedent).



- **PDF attachments reach the model as real text, not mojibake** (#738) — the attachment
  expander decoded every non-image file as UTF-8, so a PDF arrived as `[file: report.pdf]`
  followed by thousands of replacement characters — the same noise images used to produce
  (#633), just mistaken for text instead of flagged as binary. A new `document_extraction.py`
  seam (`pypdf`, pure-Python, no system deps) reads a PDF's real text layer instead, page by
  page (`[page N]` markers), bounded to 20k characters with a truncation note. An encrypted
  (beyond an empty/owner-only password) or scanned (image-only) PDF renders an honest metadata
  block — `[file: report.pdf — PDF, 12 pages, no extractable text]` — never mojibake, and the
  attachment is never silently dropped either. The same honest-block treatment now catches any
  *other* file whose UTF-8 decode turns out mostly replacement characters (a zip renamed
  `.bin`) — no format gets to pretend binary is text. Out of scope, deliberately: OCR, docx/pptx
  extractors (the seam is built to make adding one trivial, not built yet), and native PDF
  pass-through for a hosted model that accepts documents directly. `core-app` 0.98.0→0.99.0
  (MINOR — a new `pypdf` dependency; no web change).


- **A flaky `AutomationsScreen` test under full-suite CPU contention** (#758) —
  `"toggles an automation without a reload"` reached for a `getByRole("switch", …)` query
  as its very first assertion after render, with no cheaper checkpoint first; a role query
  computes accessible names over the whole tree and is measurably pricier than a text
  match, so it timed out twice in full-suite (`npx vitest run`, no file filter) background
  runs while every sibling test in the file either settles on plain text first or (in one
  case) already follows that exact pattern. It always passed cleanly in isolation — a
  timing symptom of many parallel worker processes contending for CPU, not a logic bug.
  Fixed the test to check text first, matching its own sibling, and raised testing-library's
  global `asyncUtilTimeout` from the 1000ms default to 3000ms in the shared test setup — the
  more general fix for the same class of contention-induced timeout anywhere else in the
  suite, while still failing a genuinely-hung query promptly. `web` 0.125.0→0.125.1 (PATCH).

- **Editor: Knowledge's "New document" can now be named at creation** (#740) — it used to
  materialize as `new-note.md` (then `new-note-2.md`, …) with no chance to name it: the
  `can_create` flow (Notes) already prompted for a name and slugified it, but Knowledge's
  `can_manage_files` doors — the root "New document" and the in-folder create — hardcoded the
  slug instead, and rename couldn't rescue it because an unsaved doc isn't in the tree yet (a
  `moveItem` call against it would 404 before the first save). Both Knowledge doors now open
  the same naming step Notes has: the root door reuses its toolbar form, and the in-folder door
  gets a new inline row right in the tree at the folder you clicked, rather than a form that
  pops up disconnected from it in the toolbar. A single shared submit now backs all three doors
  (`can_create`'s form and both `can_manage_files` doors), slugifying the name and seeding the
  same `# <name>` heading (Knowledge's slugs keep the `.md` extension its file store has always
  used; Notes' never have had one) — and lands in preview per #729, same as any other door.
  Rename is now safe for an unsaved document too: the shell shows a just-created, not-yet-saved
  doc in the tree (Knowledge only — Notes never exposes tree actions) precisely so its hover
  rename/delete are reachable, and both are guarded to act **locally** — swap the slug, abandon
  the draft — instead of firing a server call that would 404; once the first save lands, both
  fall through to the existing server-backed behavior unchanged. Incidental fix while touching
  the shared slug helper: `uniqueSlug`'s collision suffix now lands before a `.md` extension
  (`name-2.md`) instead of after it (the pre-existing, never-before-exercised `name.md-2`).
  `web` 0.123.0→0.123.1 (PATCH).



- **Editor: create lands in preview — render-first now applies to creation, not just
  opening** (#729) — creating a document (Knowledge or Notes) opened it in **edit** mode,
  while opening an *existing* document landed in **preview** (render-first, ADR-0042); create
  was the one door that showed a raw, unrendered buffer where every other door showed the
  rendered result immediately. The archetype flipped to preview only on the doc-fetch seeding
  path, and all three create doors explicitly set edit mode (and skip that fetch via `isNew`,
  so the flip never ran): the plain "New document", the named "New note" (Notes' `can_create`
  flow), and "New file in folder" — the command palette's `?new=1` deep-link funnels into the
  same doors. All three now land in preview like every other open; Edit stays one click away
  on the mode toggle. The two doors that seeded an empty buffer ("New document", "New file in
  folder") now seed a `# Untitled` heading — matching the Notes service's own "no heading
  found" fallback (`derive_title`) — so the first preview never renders a blank pane; the named
  door already seeded `# <name>` and keeps doing so. One archetype change covers both pages
  (Notes and Knowledge share the `editor` archetype, ADR-0018). `web` 0.122.0→0.122.1 (PATCH).
- **KV-cache fallback message: a staged choice needs a restart, not an environment edit** (#709)
  — `apply_kv_cache_type` has two distinct degraded modes and the API collapsed both into
  `applied: false`, so the Models page always showed the scarier, mostly-wrong instruction
  ("set `OLLAMA_KV_CACHE_TYPE` … in your environment"). In the common case — the env file was
  written and only Docker access is missing — that instruction is busywork: Ollama's entrypoint
  re-sources `/etc/epicurus/ollama.env` on every start, so a plain container restart applies the
  already-staged choice. The prefs route now returns `staged` beside `applied`
  (`applied` ⇒ `staged`; a `KvCacheApplyResult` replaces the bare bool), and the UI branches on
  it: staged → "restart the Ollama container to apply … `docker compose restart ollama`";
  not staged (the env file could not be written at all) → today's environment-variable text.
  The clear-to-default path stages identically — a successful unlink *is* the choice on disk.
  A core predating this reports no `staged`, which defaults to `false`, so an older backend keeps
  the old copy rather than promising a restart that wouldn't help.
  `core-app` 0.93.0→0.93.1 (PATCH) · `web` 0.120.0→0.120.1 (PATCH).
- **Model catalog: re-anchored on the library's current markup** (#710) — the public model
  library dropped the `x-test-*` attributes the parser keyed on, so every refresh parsed to
  `[]`, the box served the seed indefinitely, and a `model catalog refresh failed` warning was
  written every `refresh_seconds` for weeks. The parser now keys on the page's **structure and
  user-visible copy** — the per-model `/library/<name>` link, the rounded-badge idiom, and the
  word "Pulls" — and classifies each chip by its *text* rather than by the Tailwind colour that
  distinguishes capabilities from sizes, so a restyle degrades instead of silently mislabelling.
  Three regressions fall out of the live data: mixture-of-experts (`8x7b`, `128x17b`) and
  "effective" (`e2b`) size labels are now expanded into pullable entries instead of dropped;
  comma-grouped pull counts (`8,171`) rank correctly instead of scoring 0; and a blurb that
  merely *mentions* "updated"/"tags"/"pulls" is no longer blanked (the stats line is told apart
  structurally now — this affected 7 of the library's 233 families, `mistral` among them).
  Repeated failures are **bounded**: the first failure of a streak warns, a *changed* error
  warns again, the rest drop to debug, and the recovery reports how long the outage ran.
  `tests/fixtures/ollama-library.html` and `ollama-tags-*.html` are trimmed verbatim captures
  (2026-07-25) that pin the real markup, so the next redesign fails a test instead of the box.
  The tags-page selectors (#571, #330) were verified against the same redesign and needed no
  change — they key on the `/library/<family>:<tag>` link, which survived. `core-app`
  0.92.0→0.92.1 (PATCH).

### Changed

- **Agent grounding: local sources first, then the web — never an unsourced guess** (#703) — the
  shipped default instructions (#497) gain a "Finding answers" source ladder: the operator's module
  data (knowledge base, notes, calendar, tasks, mail, files) first, then `web_search` when local
  sources come up empty or a fact may have changed since training, and an explicit never-guess rule
  (answer from a source, or say plainly nothing was found). `web_search`'s tool description now says
  *when* to reach for it, not just what it does. Tenants with a customized prompt keep their text —
  the paragraph can be adopted via Settings → Assistant instructions. `core-app` 0.91.0→0.92.0
  (MINOR) · `websearch` 0.2.0→0.2.1 (PATCH).

### Added

- **`set_chat_model`: switch a conversation's model by asking** (#707) — "answer with grok from
  now on" now works. The new core tool resolves the request against exactly what the web's model
  picker offers — installed local models and the tenant's saved hosted models — case-insensitively,
  exact match first then a substring match but only when it's unique; an unknown or ambiguous name
  changes nothing and returns the available list rather than guess. The choice persists to a new
  `session_models` sidecar table (there is no other "session row" — a session is derived from
  `agent_messages` via `GROUP BY`) and survives a reload; `GET /sessions` reads it back for the
  picker, and an explicit picker change writes the *same* field via a new
  `PUT /sessions/{id}/model` (`model: null` clears it back to the device default) — one owner of
  truth, so a tool switch and a manual pick can never fight. The **server** resolves that row at
  turn time (`/chat`, `/chat/stream`, `/regenerate`, `/edit`), so `model` on a request is the
  caller's *default* and the conversation's own choice wins whenever it has one. Resolving it
  client-side would have left a window: a turn sent between the tool writing the row and the
  client's cached sessions list refreshing would have run the old model, silently — the agent
  says it switched and the next answer proves otherwise. A store hiccup degrades to the
  caller's default rather than costing the turn. Classified `write`, following the
  ordinary autonomy dial like `remember`/`ask_user` rather than a bespoke automation exclusion; it's
  naturally inert for an automation with no chat sink (no session to apply it to). `core-app`
  0.95.0→0.96.0 (MINOR) · `web` 0.121.0→0.122.0 (MINOR). ADR-0111.

- **Least-privilege Docker control, on by default** (#708) — a KV-cache change now applies
  immediately and a removed module's container is torn down at once, out of the box, with no
  operator setup. `core-app` reaches Docker through a new `docker-proxy-core` allowlist proxy
  (`wollomatic/socket-proxy`) instead of a raw socket — scoped to exactly the calls
  `DockerController` makes (list/inspect a container, stop/restart/remove one by id), refusing
  exec/create/attach/images/volumes/networks/system regardless of who asks. The edge gateway
  gets its own read-only sibling, `docker-proxy-traefik` (list/inspect/events/version, no write
  verb at all), replacing its previous raw `:ro` socket mount — implementing the socket-proxy
  option #656 proposed (final posture confirmed at review). The pre-existing raw-socket overlay
  (`services/core-app/compose.docker-socket.yaml`) remains as a documented escape hatch for an
  operator who'd rather core-app hold the socket directly. `core-app` 0.94.0→0.95.0 (MINOR).

- **Agent-gated delivery: a `quiet` run outcome the model opts into** (#706) — a per-automation
  "agent decides delivery" toggle (`agent_gated_delivery`, off by default) offers the run's turn a
  run-scoped `finish_quiet(reason)` tool, bound at the tool surface — spliced into `Agent._loop` only
  when both `automation_id` and the toggle are set, never a global built-in, so it can never reach an
  ordinary chat turn. Calling it marks the run outcome `quiet` and skips the push/notes/kb sink
  fan-out; not calling it delivers exactly as before (fail-loud beats fail-silent). The ledger still
  always records the run, with the model's own reason (`AutomationRun.quiet_reason`); `chat` is
  deliberately exempted (it is turn-time — ADR-0108 — and rolling continuity needs the next run to
  see this one's reply regardless). The Automations editor gains the toggle; the runs feed and the
  per-automation history badge `quiet` outcomes distinctly with their reason. `core-app` 0.93.1→0.94.0
  (MINOR) · `web` 0.120.1→0.121.0 (MINOR). ADR-0110.

- **Propose-autonomy automations can now actually reach mail's compose tools and knowledge/
  notes' suggestion tools** (#721) — completes the sweep #705 started: `mail_send`/
  `mail_reply`, knowledge's six `knowledge_propose_*`/`knowledge_create_document`, and notes'
  four propose-shaped tools now declare `side_effect="propose"`. Previously unannotated
  (defaulting to `write`), a `propose`-autonomy automation had zero usable tools on any of
  these three modules — the tier was structurally dead for them. Knowledge's and notes' tools
  stage a suggestion unless the operator has turned suggestion review off for that module, in
  which case they apply directly, same as they already do in chat (ADR-0112 documents why this
  is an accepted interaction with an existing per-module setting, not a gap the annotation
  should paper over); `mail_send`/`mail_reply` carry no such caveat — they compose a draft
  unconditionally, and an unattended automation turn already degrades a draft it can't show
  into an informative error rather than ever sending. `mail` 0.15.0→0.16.0 · `knowledge`
  0.25.0→0.26.0 · `notes` 0.10.0→0.11.0 (all MINOR). No `core-app` change.

- **Starter automation templates for every module** (#705) — the Templates tab was
  effectively empty (only `echo`'s reference `on-ping`); mail, calendar, tasks, notes, and
  knowledge each now declare two curated presets — mail (triage new mail, morning unread
  digest), calendar (tomorrow-at-a-glance, event-starting-soon — "notify on a new invitation"
  was dropped, no such event exists yet), tasks (due-today digest, overdue alert), notes
  (weekly review, on-note-created), knowledge (large-vault-sync notify, index-failed notify).
  All ten are `notify`-autonomy with a `push` sink, never auto-instantiated (the operator
  instantiates from the Templates tab, same as ever). Getting these actually useful required
  annotating each module's read tools `side_effect="read"` (`mail_search`/`mail_read`,
  `calendar_list_events`/`calendar_find_free`, `tasks_list`/`tasks_lists`, `notes_list`/
  `notes_tree`, `knowledge_search`/`knowledge_list_projects`/`knowledge_tree`/
  `knowledge_read_document`) — unannotated defaults to `write`, which a Notify automation can
  never call, so a schedule-triggered template (no triggering-event payload to fall back on)
  would otherwise reach zero tools and produce nothing useful. `mail` 0.14.0→0.15.0 ·
  `calendar` 0.17.0→0.18.0 · `tasks` 0.17.0→0.18.0 · `notes` 0.9.1→0.10.0 · `knowledge`
  0.24.1→0.25.0 (all MINOR). No `core-app` change — the templates contract already existed
  (ADR-0105).

- **Automations completion: conversational drafting + the three sinks** (#667, #672) — closes the
  automations loop opened in W7. `propose_automation` is a core built-in that drafts an automation
  from a natural-language ask and **only stages** it as a `ReviewSuggestion` — a hard guardrail with
  no path to create or enable at any autonomy. Approval on a **second in-process `core` review page**
  (a `CorePages` composite declaring both `PageSpec`s; `ModuleRegistry` already fans out over a
  manifest's `pages`, so no registry change) is the one create path, and it creates the automation
  *enabled* — the review is the consent. The review modal gains an editable **model picker** over an
  additive `ReviewSuggestion.automation` (`AutomationPreview`). The engine's sink seam (ADR-0105, left
  registered but empty) is now wired under the owner rules: **chat is turn-time** — the runner passes
  `session_id` only when the chat sink is set, so an unchecked chat sink means zero sessions; a new
  `automation_sessions` table badges those chats and groups a per-run automation's chats, and the
  post-run dispatcher **skips** chat. **Notes/KB** route through the existing
  `ModuleRegistry.save_page_doc` (the #541 no-second-write-path rule) at a per-automation
  `DocumentTarget` (a `path_pattern` with `{date}`/`{datetime}`/`{time}` tokens, create or append);
  each write records an `EntityRef` on the run's new `automation_runs.artifacts` column, so the runs
  feed (#688) links what was produced. `automations.sink_config` and `automation_runs.artifacts` are
  the first columns added since #682, so `AutomationStore.init` now runs `ensure_columns` (ADR-0067).
  Implements ADR-0107 and ADR-0108. `epicurus-core` 0.30.0→0.31.0 (MINOR) · `core-app`
  0.89.0→0.91.0 (MINOR) · `web` 0.116.0→0.118.0 (MINOR).
- **Web: the Automations page** (#668, stacked on #669) — the operator's controls for the
  engine, a first-class core surface beside Settings. The list reads each row **in words**
  ("When mail.received arrives, matching subject contains invoice, between 09:00–17:00" ·
  "Weekly on Tuesday at 09:00") with an autonomy badge, sink icons, an enabled toggle that
  takes effect without a reload, and last-run status; the tenant-wide **kill switch** is
  pinned above everything. One Sheet editor serves create / edit / instantiate-a-template and
  edits **every field the engine stores** — instructions, a per-automation model
  (core-default fall-through, local + hosted lists), an event trigger whose type picker is
  driven by the live event catalog (module manifests' declared `events.*` subjects, with a
  free-text escape hatch for the core's own `files.*`/`core.*` families) plus the matcher
  builder and active-hours window, or a schedule in the ADR-0092 cadence/hour/weekday
  vocabulary; sinks + chat mode; the 4-level autonomy dial with its reach spelled out; rate
  cap and digest window. Saved **explicitly** (the fields are interdependent — the ADR-0098
  posture), and a server-rejected save shows its reason inline with the sheet still open.
  The **Templates** tab groups module presets by module; *Use* prefills the editor and
  saving creates an independent `source="template:<module>"` row — **enabled on save** (the
  editor pass is the review; owner-decided) and never retro-edited by later template
  changes (provenance is not editable, enforced server-side). Per-row **Run history** reads
  the ledger and deep-links into the runs feed (`/observability?tab=runs&automation=<id>` —
  the observability screen learned to open on a requested tab), and the feed's automation
  badge now links back. **Run now** exercises the real runner. The engine gains the missing
  management endpoint — `PUT /platform/v1/automations/{id}`, create-shape validation before
  any write, `source`/`chat_session_id`/`created_at`/last-run stamps never touched — and
  `AutomationStore.update`. The old **scheduled-turns Settings card is absorbed**: the card,
  its api client and contract are gone (the engine already migrated the rows and keeps the
  old endpoints answering); migrated rows simply appear as automations. `web`
  0.115.0→0.116.0 (MINOR) · `core-app` 0.88.1→0.89.0 (MINOR — the management endpoint).
- **Observability: the Automation runs feed** (#669, stacked on #666) — the third live tab,
  and the engine's glass: a triggered run is traceable end-to-end in one place, fire → filter
  verdict → run (model, tokens, duration) → sinks delivered / error. **Skips are the point**:
  a rate-capped or paused run appears as loudly as a real one with its *why* inline ("rate cap
  reached (4/hour)", "runtime paused") — a cap being hit should be visible, not inferred from
  an automation that mysteriously went quiet — and a `silent_act` run, whose only trace is the
  ledger, is visible here too. Backed by `GET /platform/v1/automations/runs/stream` (SSE,
  history-then-live per the ADR-0031 console shape; a new `RunFeed` fans out every ledger entry
  the moment the runner's new `on_recorded` hook records it) and an `outcome` filter on the
  list endpoint (`ok`/`error`/`skipped`, server-side). Each run's `trigger_refs` come back as
  `trigger_entity_refs` — the triggering events' `EntityRef`s resolved from the event log
  (`EventLogStore.by_ids`) — so rows carry ADR-0019 hover-card chips to the source entities
  with no per-module code. The tab filters by automation and outcome server-side (they
  re-subscribe) and by trigger module client-side over the automations list (a run itself
  carries no module), reusing `useSseFeed` exactly as the Logs and Events tabs do; the
  automations list also names each run. `web` 0.114.0→0.115.0 (MINOR) · `core-app`
  0.88.0→0.88.1 (PATCH — endpoints and hook exist to serve the new surface, per the issue's
  own bump call).
- **Automations: the engine** (#666, ADR-0105) — the centerpiece of event-driven proactivity,
  and what the event spine exists to feed: modules announce that the world changed, and this
  decides whether the assistant should do anything about it. A tenant-scoped `automations` row
  is a **trigger** (an event — module + type + a deterministic payload filter + an optional
  local-hour window — *or* a schedule, reusing the ADR-0092 cadence vocabulary), an **agent
  step** (prompt + optional per-automation model + autonomy level), **sinks**, a rate cap, and
  a digest window. **The 4-level autonomy dial is structural, not persuasive.** A Notify
  automation is not asked to avoid writing — it is handed no tool that can. That needed a
  vocabulary the codebase didn't have, so tools now declare `side_effect` on their `ToolSpec`:
  `read` (observes), `propose` (**stages for approval by construction** — a draft-first send,
  a propose tool that files a suggestion), `write` (applies directly). Three classes, because
  two collapse the dial: with only read/write, Propose and Act get identical surfaces and the
  middle of the dial becomes prompt wording again. It is *declared*, never inferred —
  `mail_mark_read` contains "read" and mutates — and **defaults to `write`**, so a forgotten
  annotation costs availability, never containment. Enforcement filters the `route` the agent
  dispatches on, not just the specs the model is shown: a withheld tool is **unroutable**, and
  a model that names it anyway is told `error: unknown tool`. Levels: notify→read ·
  propose→+propose · act→+write · silent_act→ the same reach as act, reporting **only** to the
  ledger. The runner matches on the event intake, queues durably (ADR-0051), batches a digest
  from the *oldest* pending trigger, runs one agent turn with the triggering events as
  **context, explicitly not instructions**, then fans out to sinks **deterministically after**
  the turn — a model that could choose its own reporting could choose not to report. Safety:
  a **Postgres-persisted** kill switch (unlike the in-memory power pause — a stop a restart
  undoes is not a stop), rate caps that a *failing* run also consumes, a rate-limited
  `core.automation_failed`, and a **depth-1 loop guard** — an event a run produces carries a
  `causation_id` and the matcher refuses *any* event carrying one, because A→B→A is a loop too. `core-app` 0.87.0→0.88.0, `epicurus-core` 0.29.0→0.30.0, `echo` 0.4.0→0.5.0 (all MINOR).
  `automation_runs` always records, at every level, with **dual metering**: tenant *and*
  automation, on the ledger and on `UsageEvent`, since an automation quietly burning tokens is
  otherwise indistinguishable from the operator's own chatting. **Scheduled turns (#614) fold
  in** — migrated at startup, idempotently and non-destructively, keeping cadence, session,
  enabled flag and last-run stamp, at `notify` because that is what they already were. Modules
  may declare `automation_templates`; they are **never auto-instantiated** (the contract
  carries no `enabled` field to set).
- **Events: the module event spine** (#662, ADR-0103) — the keystone of event-driven
  proactivity: modules announce that the world changed, the core keeps the copy of record, and
  the automations engine (a companion issue) decides whether anything should happen about it.
  Nothing consumes events yet, which is exactly why the contract is fixed now — every emitter
  and every consumer will be written against it. **One envelope** in `epicurus_core.module_events`
  (`tenant_id · module · type · occurred_at · dedup_key · entity_ref? · payload ·
  schema_version`) plus an `emit_event()` helper over the existing NATS plumbing, on a dedicated
  `events.` subject namespace (`<tenant>.events.mail.received`) so the spine is subscribable as
  a whole while the bus's existing per-module subjects keep their names. Payload discipline —
  **pointers and metadata, never content, never secrets** — is *enforced*, not requested: a
  4096-byte cap a mail body cannot fit through, and rejection of credential-shaped payload keys
  (an over-match by design: `idempotency_key` is refused exactly like `api_key`, because a false
  positive costs a rename and a false negative leaks a credential to a browser tab). A `type`
  must be prefixed with its own module, so a subject is self-describing and a typo fails at emit
  rather than mis-attributing an event forever. **Durable intake** in core-app subscribes
  `*.events.>` — one subscription, *every* tenant (`EventBus.subscribe_any_tenant`), departing
  from the inbound-messaging consumer's per-tenant subscribe, which would leave a tenant added
  at runtime silently unheard until restart. Each message carries two independent tenant claims
  (the subject's and the envelope's); a mismatch is dropped rather than filed under a guess.
  Events land in a tenant-scoped `module_events` log, deduped by a database constraint on
  `(tenant, module, dedup_key)` with **first write wins** (a later delivery of an
  already-recorded change carries no newer truth), bounded by `EVENTS_RETENTION_DAYS` (30;
  `0` keeps everything). Delivery is **best-effort v1** — core pub/sub, at-most-once; JetStream
  is enabled on the server and deliberately unused, which is *why* the log rather than the bus
  is the copy of record. A **raw Events feed** joins the log console on the Observability screen
  behind a new tab strip (the automation-runs feed lands as a third tab); the ADR-0031 redaction
  rule moved into `epicurus_core.redaction` so both surfaces share one list rather than drifting.
  **echo** gains `echo_ping` / a "Ping the spine" action emitting `echo.pinged` — the reference
  emitter — and `task smoke` now asserts the whole chain end-to-end on a fresh stack: emit →
  NATS → intake → durable log → feed, plus the log's idempotency. `docs/reference/events.md`
  starts **the event catalog**; real module emitters extend it.
- **Mail: the first real module emitters — `mail.received` / `mail.sent` / `mail.sync_failed`**
  (#663, stacked on #662) — mail's cache reconcile (ADR-0096, #623) is the one place a
  genuinely-new message, as opposed to a flag flip, is already known, so that's where all three
  now fire. `mail.received` needed message-granular detection the sync seam never had: Gmail's
  `changed_threads_since` was thread-granular only (`_history_thread_ids` conflates
  `messagesAdded`/`messagesDeleted`/`labelsAdded`/`labelsRemoved` into one set), so
  `ThreadChanges` gains a narrower `new_message_ids` field, filled from `messagesAdded` history
  records specifically — a flag flip on an existing message never fires it. Each new message gets
  one `provider.read()` (thread-summary data reflects only a thread's *latest* message, wrong the
  moment two new messages land in the same thread within one reconcile window) for an accurate
  `from`/`subject` (capped)/`folder`/`has_attachments` payload — `MailMessage` gains its own
  `label_ids` so `folder` reflects the message's real placement, not whichever label triggered the
  poll. **No-firehose, by construction, not by a special case**: a cold cache and an
  expired-cursor forced resync both already route through the same `_full_sync` path, which the
  reconcile loop never touches for `mail.received` — the initial/full-sync case costs no new code.
  `mail.sync_failed` fires on a provider/auth error or an expired sync cursor, rate-limited per
  instance (`MAIL_SYNC_FAILED_COOLDOWN_S`, default 900s) so a flapping account can't storm the
  bus. `mail.sent` — previously declared but publishing on the bare `mail.sent` subject via a raw
  `EventBus.publish` with an ad-hoc, uncapped payload — now rides the same spine as its two new
  siblings. IMAP does not exist in this codebase yet (only `GmailProvider` is implemented,
  despite the module's provider-neutral design); the catalog's provider-caveats section notes
  what a future IMAP provider would need to fill. `mail` 0.13.0→0.14.0 (MINOR).
- **Calendar & Tasks: lifecycle + lead-time events** (#664, stacked on #663) — "two modules,
  one pattern," including the new lead-time scheduler both now share the shape of. **Calendar**
  gains `event_created`/`event_updated`/`event_cancelled` on `CollectionRouter`'s
  create/update/delete_event — the **provider-write** seam only, since calendar has no
  sync/reconcile layer analogous to mail's (ADR-0096); a change made directly in Google
  Calendar's own UI is never observed. `event_updated`'s `time_changed` flag is a real
  before/after comparison (the router snapshots prior state first), and its `dedup_key` folds
  in a change hash of the event's mutable fields, so a genuinely different edit is its own log
  entry while a retried write with identical resulting state dedups — the same "dedup provider
  id + change hash" posture `tasks.task_updated` now also uses. `invitation_received` /
  `attendee_responded` (Google-only, ADR-0030) are **deliberately not implemented**: both need
  the same kind of external-change detection a sync layer would provide, a materially larger
  feature than wiring emission into an existing seam — declaring either without actually
  publishing it would repeat mail's own "declared but never emitted" mistake rather than avoid
  it. **Tasks** gains `task_created`/`task_completed`/`task_updated` on `TasksRouter`'s
  add/complete/update_task, and `task_moved` on the ADR-0038/#257 cross-list seam (fired
  instead of `task_updated`, never alongside it — Google Tasks has no move API, so a move
  recreates in the target and deletes the source). A recurring task's auto-materialized
  successor (`_materialize`, ADR-0082) calls the inner provider directly, bypassing the
  router's own `add_task` — a deliberate scope limit, so it does not currently emit
  `task_created`. **The lead-time scheduler** is a new periodic background job — the first for
  either module — durably fire-once via a `(tenant, entity_id, marker)`-unique marker table
  (`BigInteger` epoch column) proven to survive a restart; each module also gains a
  settings-primitives-shaped tenant preference for its own lead (calendar: minutes, default
  15; tasks: days, default 1 — storage only, no settings UI yet). Calendar's lead is pure
  instant math; tasks' `task_due_soon`/`task_overdue` evaluate against the *operator's local
  calendar day* (ADR-0039), reusing the exact `operator_clock` the overdue-recurrence sweep
  already resolves, so the two can never disagree about what day it is. `calendar`
  0.16.0→0.17.0 (MINOR) · `tasks` 0.16.0→0.17.0 (MINOR).

- **Notes, Knowledge, Files: content events + suggestion-decision events** (#665, stacked on
  #662) — the operator-content half of the emitter sweep. **Notes/knowledge doc events**:
  `note_created`/`doc_created` and `note_deleted`/`doc_deleted` fire immediately at the change
  (editor, file tree, or an approved suggestion — both authors converge on the same pages
  seams), but `note_updated`/`doc_updated` are **debounced to settled saves**: the ADR-0042
  auto-save fires a PUT on every ~4s idle pause, so each save re-arms a per-document quiet
  window (`NOTES_EVENTS_DEBOUNCE_S` / `KNOWLEDGE_EVENTS_DEBOUNCE_S`, default **120s**) and one
  event fires when it passes untouched — carrying the *last save's* timestamp as `occurred_at`
  and the count of saves coalesced. The debounce is a swept dict driven by a pure
  `flush_due(now)` (the ADR-0092/0098 test idiom), duplicated per module rather than landed in
  the high-contention core lib (rule of three; the engine PR is already bumping it); pending
  entries flush on shutdown, deletes cancel them, renames re-key them (knowledge folder moves
  re-key by prefix). `knowledge.vault_synced` is **one batch event per watcher pass** with the
  pass's honest counts (`indexed` merges added+updated — the walk doesn't distinguish); no-op
  passes and the startup index emit nothing (no-firehose). `knowledge.index_failed` is
  rate-limited (`KNOWLEDGE_INDEX_FAILED_COOLDOWN_S`, 900s, the mail.sync_failed posture) and
  fires on the initial index giving up (#230's retry budget) or a watcher pass failing.
  **Files events are core-emitted** (the core owns the file space, #434) at the file-API seam:
  `files.file_added` (upload, or a module/agent write of a genuinely-new path),
  `files.file_deleted` (one per API action — a folder is one event), `files.file_moved`
  (file-space + object-store fallbacks). Deliberately **no `file_updated`**: an overwrite emits
  nothing, so mirrored module content doesn't double-signal its own `*_updated` here.
  **Suggestion decisions are core-emitted at the one review funnel**
  (`ModuleRegistry.review_action`): `core.suggestion_approved` / `core.suggestion_rejected`
  fire once per decision whether the operator used a module's review page (HTTP-proxied) or
  the core-hosted pseudo-module surface (ADR-0093 §2), with `operation`/`path` lifted from the
  surface's `ApplyResult`. Two legacy subjects retire: notes' bare `notes.saved` publish (no
  consumer — the mail.sent migration, #663) and knowledge's declared-but-never-published
  `knowledge.index.completed` (the manifest now only advertises events that fire). All emission
  is best-effort — a spine hiccup never fails the save, delete, or decision that already
  landed. `notes` 0.8.0→0.9.0 (MINOR) · `knowledge` 0.23.0→0.24.0 (MINOR) · `core-app`
  0.86.0→0.87.0 (MINOR).

  starts **the event catalog**; real module emitters extend it. `epicurus-core` 0.28.0→0.29.0
  (MINOR), `core-app` 0.85.0→0.86.0 (MINOR), `web` 0.113.0→0.114.0 (MINOR), `echo`
  0.3.0→0.4.0 (MINOR).
- **Push: web push end-to-end** (#670) — VAPID-signed browser push, the "push alerts" half of
  event-driven proactivity. A tenant's VAPID keypair is generated on first send and stored in
  OpenBao — no operator provisioning step. Per-category + quiet-hours preferences
  (`push_prefs`, shared with the notification center, #671) gate every send: category off
  skips it, quiet hours (tenant timezone, ADR-0039) queue it for one summary digest once the
  window ends rather than dropping it, and an in-memory per-tenant rate cap is the last gate.
  A subscription the push service reports Gone (404/410) is pruned automatically.
  `services/web/src/sw.ts` gains `push`/`notificationclick` handlers; a new Settings → Push
  notifications card handles this-device subscribe/unsubscribe, per-device management,
  category toggles, quiet hours, and a test-notification button. `PushService.notify()` is a
  core-internal contract (no HTTP route) for the automations engine's future push sink and
  system notices — today's only caller is the settings UI's test button. Desktop Chrome/Edge
  and installed Android/iOS PWA (16.4+) both work. Implements ADR-0102. `core-app`
  0.83.0→0.84.0 (MINOR), `web` 0.111.0→0.112.0 (MINOR).
- **Push: in-app notification center** (#671) — the durable record every push-worthy
  notification lands in, written by `PushService.notify()` itself the instant a category's
  `center` toggle is on — independent of whether push delivery fires, queues for quiet
  hours, or is itself disabled, so a quiet-hours-suppressed push still appears in the center
  immediately rather than waiting for the digest. A new **Notifications** page (list, category
  filter, unread-only filter, mark read / mark all read, `EntityRef` hover-cards + deep links
  via the existing `CardLink`) and a live unread-count badge on its own nav entry (polled every
  15s, the same shape as the #492 "finished while you were away" watcher). Retention is a
  per-tenant row cap (500), not time-based. Stacks on #670's shared `{push, center}` prefs
  object — no second settings surface. Implements ADR-0104. `core-app` 0.84.0→0.85.0 (MINOR),
  `web` 0.112.0→0.113.0 (MINOR).
- **Agent: nightly reflection proposes playbook/instruction edits** (#615) — the other half of
  governed playbooks: what actually notices a lesson worth keeping. A new `playbook-reflection`
  job on the maintenance orchestrator's nightly batch (additive — one entry appended to the
  registry, registered `nightly=True`, so it rides the orchestrator's existing schedule rather
  than a knob of its own). Per tenant it scans the sessions active since its last run and makes
  **one** gateway call over them, staging zero or more candidate edits — to the base instructions
  or a named playbook — as review proposals. It **cannot apply anything**: it is handed a proposal
  sink and a read-only playbook lookup, never the stores that own the documents, so ADR-0093's
  "nothing self-applies" is enforced by construction. The call is metered under **the tenant whose
  sessions it scanned**, never the default tenant (constraints #1/#8, the ADR-0051 drain's
  precedent). Recently **rejected** proposals are digested into the prompt as negative context
  from the ADR-0090 audit trail, so a declined idea isn't re-proposed unchanged; a document with a
  proposal still pending is skipped, so the queue can't stack drafts while the operator is away.
  The create-vs-update operation is derived from what exists rather than taken from the model — a
  mislabelled create would render an empty *current* side and hide what an approval would
  overwrite. A durable per-tenant watermark (`agent_reflection_state`) bounds the scan, snapshotted
  before it and advanced only on a completed pass. Junk costs nothing: a non-JSON reply, an unknown
  target, or a runaway generation stages nothing rather than raising, and a tenant with no new
  activity spends no gateway call at all. New `PLAYBOOK_REFLECTION_MODEL` (blank = the default chat
  model). Implements ADR-0093 §1/§5/§6. `core-app` 0.80.0→0.81.0 (MINOR).
- **Agent: governed playbooks — storage + a core-hosted approval surface** (#616) — the agent's
  behaviour improved only when the operator hand-edited the base prompt; nothing captured what the
  system learns in use. **Playbooks** are named, independently enable-able blocks of guidance
  stored beside that prompt (`agent_playbooks`) and composed into every turn — base first, then
  each enabled playbook under its own heading. The composition happens **below**
  `AgentInstructionsStore.get_instructions`, so `Agent._assemble` is untouched and still leads the
  turn with one opaque string; a failed playbook read degrades to the base prompt rather than
  costing the turn. Nothing self-applies: an edit arrives as a `ReviewSuggestion` (ADR-0090) and
  only the operator's **Approve** writes it — through the existing instructions store for the base
  prompt (the same path a manual Settings edit uses), or the playbook store for a named one. Both
  halves gained ADR-0046 snapshot-on-save versioning (`agent_instructions_versions`,
  `agent_playbook_versions`; capped at 50, oldest pruned), snapshotting the body each save
  *replaced* so an approved agent-authored edit is always undoable. The approval UI is the
  existing, unmodified `ReviewView`/`SuggestionReviewModal`: the core registers a reserved **`core`
  pseudo-module** that `ModuleRegistry` answers **in-process** (no loopback HTTP), riding
  `GET /platform/v1/modules` so the shell discovers its `review` page like any module's. It is
  deliberately *not* a configured base, so it can never leak into the agent's MCP tool surface or
  the re-embed fan-out; `enabled` / `DELETE` / `suggestions-enabled` all **403** for it — its
  review is mandatory. Web-side, the Suggestions inbox shows *Always reviewed* instead of a toggle
  for that group, and the Modules screen filters the reserved name out. Implements ADR-0093
  §2/§3/§4. `core-app` 0.79.0→0.80.0 (MINOR), `web` 0.109.0→0.109.1 (PATCH).

### Fixed

- **Web: no more cold-mount flash on a folder/search/pagination switch** (#712) — mail's
  thread list blanked to a spinner on every folder change (search, and pagination too),
  because each switch is a new react-query key and, left at the defaults, that mounts cold
  with no data. `MailboxView`'s list query now keeps the previous folder's data on screen
  (`placeholderData: keepPreviousData`) with a small inline spinner as the only sign a fetch
  is under way, plus a tuned `staleTime`/`gcTime` so every folder visited in a session stays
  warm and a revisit renders instantly. A borrowed placeholder also has to be guarded wherever
  one query gates on another's success: the cache-first reconcile read (#623) waits on the list
  query, and a placeholder resolves that to `success` before the newly-selected folder's own read
  has landed, so the gate now also requires `!isPlaceholderData` — without it a first visit fired
  both provider reads at once, the exact double round-trip ADR-0096's gate exists to prevent.
  Auditing the other three archetypes for the same pattern found it live in two more places —
  `BrowserView` (a directory/search switch) and
  `EditorView` (opening a different document) — and fixed both; `EditorView`'s fix also
  guards the draft-seeding effect on `!doc.isPlaceholderData`, since without it the *previous*
  document's still-visible placeholder content would seed into the newly-selected path and
  strand the real content unread once it arrived. `CalendarView`/`BoardView` already did this
  (Calendar since #378/#379) and needed no change — they were the reference the audit checked
  the others against. `web` 0.118.0→0.119.0 (MINOR — the audit landed changes beyond mail).

- **CI: no gate ever checked a docs/ cross-reference — a dead one reached shipped operator
  UI** (#692). Issue #661 existed because `docs/DEPLOYMENT.md` was referenced from shipped,
  operator-facing UI copy and a compose comment while the file doesn't exist in the public
  tree (the real doc is the gitignored `.workspace/docs/DEPLOYMENT.md`); nothing caught it,
  and a generic markdown-link checker wouldn't catch the next one either — it never looks
  inside a `.tsx` file or a compose comment. New **docs-linkcheck** CI job
  (`scripts/check_docs_links.py`, stdlib-only) checks (1) every relative link between `docs/`
  pages, anchors included, and (2) a repo-relative doc path quoted in shipped source (web UI
  copy, compose comments, other top-level READMEs, `.env.example`) — excluding test
  fixtures (which commonly use plausible-looking-but-synthetic doc-shaped paths that were
  never meant to resolve) and `CHANGELOG.md` (narrates *past* fixes, not live references).
  The gate's first real run found and fixed four genuine, previously-invisible cases: a
  `docs/services/core-app.md` anchor named in two other pages
  (`../services/core-app.md#push-notifications-adr-0102`, `#raw-events-feed` missing its
  `-adr-0103` suffix in two places) that had no matching heading at all — added the missing
  "Push notifications (ADR-0102)" section documenting the `push/` package's send path and
  HTTP surface (previously undocumented entirely), and corrected the `-adr-0103` suffix — plus
  one heading-renamed-out-from-under-it link in `docs/services/mail.md` (`#operator-setup-google`
  → `#google-cloud-setup`). The two offenders this issue named as still-live
  (`.env.example`, `docs/developer/releases.md`) were already fixed by #661 before this PR;
  verified no live occurrence remains. No component bump — CI/docs only.

- **CI: no gate ever parsed a shell script with a POSIX shell** (#691). #675 shipped a bash
  array in `infra/cd/reconcile.sh` — invoked as `sh infra/cd/reconcile.sh` everywhere (its own
  header comment, the `task reconcile` Taskfile entry, and both scheduled-task lines in
  `docs/infrastructure/auto-deploy.md`), and on the deploy box's real `/bin/sh` (dash) a bash
  array is a parse error — but every check, CI included, ran under Git Bash's `sh`, which *is*
  bash, so the bug was invisible everywhere but production. New **shell-lint** CI job runs
  `shellcheck` over every `*.sh` (discovered via `git ls-files`, not a hardcoded list), with the
  shell it checks against inferred from each script's own shebang. Fixed the live mismatch this
  surfaced: `reconcile.sh`'s shebang said `bash` while every invocation and its own content
  (already rewritten to POSIX positional parameters, no arrays) said `sh` — corrected the
  shebang to match, and dropped `pipefail` from its `set` line (undefined in POSIX sh; Debian
  dash rejects it outright, which would have been the exact same silent-until-production
  failure this gate exists to catch). Also cleaned up the two other findings the gate's first
  real run turned up: `infra/backups/backup.sh`'s `ls | grep` (rewritten as a plain Python
  `os.listdir` filter) and a few intentional-but-unflagged-until-now `infra/ci/smoke.sh`
  patterns (a deliberately unquoted service-name word list standing in for POSIX sh's missing
  arrays, `CDPATH= cd`, sourcing a `mktemp` path) marked with `# shellcheck disable=SCxxxx` /
  `# shellcheck source=/dev/null` naming why. No component bump (CI/docs only).

- **Knowledge/notes: a rejected write returned a success envelope, so the live document pane
  could open on content that was never written** (#690). `knowledge_create_document` /
  `knowledge_propose_edit`'s `_stage_doc_write` (a bad path, an already-existing path) and
  their shared `_finalize` (a failed review-off self-apply), plus notes' equivalent `_stage`
  (an invalid slug, the same failed self-apply), all caught the rejection and returned a
  normal `tool_envelope` — so the call read as `is_error=False` to the agent loop, and the
  document pane (#541, ADR-0101) keys `doc.failed` off exactly that structural signal, not
  the reply text. A rejected write with review off would open the pane's editor over stale
  (update) or nonexistent (create) content. These paths now raise instead, so FastMCP reports
  `isError` and the pane correctly shows "the write failed." The suggestion itself is
  unaffected — a failed self-apply still leaves it staged, nothing is lost. Swept mail,
  calendar, tasks, and storage for the same "error path reuses the success constructor"
  shape; none had it — their write tools already raise directly, and their only
  `tool_envelope`-on-empty-result usages are legitimate (no rejection involved). `knowledge`
  0.24.0→0.24.1 (PATCH), `notes` 0.9.0→0.9.1 (PATCH).

- **Infra: `docs/DEPLOYMENT.md` was referenced from shipped operator UI and a compose comment,
  but that file doesn't exist in the public tree** (#661). The real document was always the
  gitignored `.workspace/docs/DEPLOYMENT.md` — since the repo went public, anyone following the
  Modules page's Docker-status card (#652/#622) or the `docker-socket` compose comment to
  `docs/DEPLOYMENT.md` got a 404. Both now point at `docs/infrastructure/index.md`'s
  "Docker-socket access" section, which #652's own docs already established as the real home for
  that content. Also swept two conceptual "DEPLOYMENT.md" mentions (`.env.example`,
  `docs/developer/releases.md`) that cited the same gone file for the image-pinning /
  immutable-image principle — retargeted at `docs/infrastructure/auto-deploy.md`, mirroring the
  link `releases.md` already uses two sections below for the same document. `web` 0.111.2→0.111.3
  (PATCH — the Modules page string). `core-app`'s `compose.yaml` touch is comment-only, no
  runtime effect — no bump.

- **Web: confirming a mid-history edit could trim the transcript to a state the server was never
  asked to produce** (#660). Editing a user message further back than the last turn discards every
  real turn since it, so `saveEdit()` confirms the count first — but it only guards
  `chat.streaming`/a dropped connection at the moment the dialog *opens*. A run starting elsewhere
  (another tab, a scheduled turn) or the connection dropping while the dialog sat open went
  unchecked: clicking **Resend** still applied the optimistic trim and re-ran the edit regardless.
  The dialog's own confirm now re-checks both at click time, the same guard `saveEdit()` already
  applies — blocked exactly like Cancel, leaving the inline editor open with the draft intact to
  retry. `web` 0.111.1→0.111.2 (PATCH).

- **Web: the document pane's "Review & approve" hard-reloaded the SPA, and its review-state
  query key missed the toggle's own invalidation** (#659). `Panel.tsx`'s `DocumentView` was the
  only SPA-internal hard navigation in the app (`window.location.assign`) — it dropped the live
  SSE stream for no reason (recoverable via ADR-0055 re-attach, but nothing to recover *from* if
  it just doesn't reload); now it navigates in-app via `useNavigate()` and explicitly dismisses
  the pane first (the panel is Shell-global and persists across routes, so — unlike the reload
  it replaces — nothing implicitly clears it). Separately, the pane's `["suggestionsEnabled",
  module]` query key was a duplicate camelCase cache entry alongside `ReviewView`/
  `SuggestionsScreen`'s established `["suggestions-enabled", module]`, so toggling review while
  the pane was open missed the invalidation the toggle fires — self-healed on refetch, but could
  leave the pane's "applied vs. staged" branch briefly stale. Also fixed while in the area: the
  panel store's `replace()` accepted a title update, but silently discarded it; and `EditorView`'s
  `doc` prop (the pane's applied→editor handover) had no direct test coverage. `web` 0.111.0→
  0.111.1 (PATCH).

- **Infra: `task reconcile` silently reverted the docker-socket opt-in on every deploy** (#655) —
  the #622 opt-in (`services/core-app/compose.docker-socket.yaml` + `DOCKER_GID`) only lasted
  until the next pull-based reconcile: `infra/cd/reconcile.sh` ran plain
  `docker compose up -d` with no overlay, so `core-app` was recreated without the socket mount,
  silently dropping back to deferred-teardown mode (fails safe, but the opt-in didn't stick — the
  pull-based reconcile is the actual deploy path, not a one-off `docker compose up`). Now
  `reconcile.sh` reads `DOCKER_GID` the same way it already reads `EPICURUS_VERSION` /
  `EPICURUS_TRACK_BRANCH` (env, falling back to `.env`) and includes the overlay on both the pull
  and the up when it's set — unset stays exactly as fail-safe as before. `docs/infrastructure/`
  updated to cover persisting the opt-in, not just the fresh-deploy default. Infra-only; no
  component version change.

- **Agent: nightly reflection never showed the model the document it was asked to rewrite**
  (#658) — the `update` path's system prompt demanded "the FULL new text… it replaces what is
  there," but `_build_prompt` passed only transcripts and recent rejections, and `list_playbooks`
  was used for names only; `get_base` was never called. Since `instructions` is always an
  `update` (there's nothing to create), that path asked for a full regeneration of text the model
  had no access to — mostly rejectable noise, at the cost of a real gateway call per tenant per
  night. Now the prompt includes the current base instructions (`get_base`) and every existing
  playbook's current content — the model edits real text instead of reconstructing from nothing.
  Folded in from the same review: the out-of-window `continue` in the session scan is now a
  `break` (`sessions()` is DESC-ordered, so the first miss guarantees the rest miss), and
  `test_reflection.py`'s model-override test now constructs the reflector with `model=` instead
  of poking the private attribute, so `settings.playbook_reflection_model` reaching the
  constructor is actually covered. `core-app` 0.83.1→0.83.2 (PATCH).

- **Core: a DB error on the reserved review page emptied the entire Suggestions inbox** (#657) —
  `ModuleRegistry._core_suggestions` caught only `HTTPException`, but the core review page is
  dispatched **in-process** (no loopback HTTP), so a storage failure — e.g. a degraded startup
  that left `playbook_proposals` uninitialized — surfaced as the driver's own exception and
  escaped the handler, 500ing `GET /platform/v1/suggestions` and taking every module's pending
  suggestions down with it, not just the core's own. Now catches bare `Exception` and logs a
  warning instead, matching the precedent in `agent/instructions.py`'s enrichment fallback — a
  broken core queue drops only its own entry, everything else in the feed survives. Also closed
  the matching read/write asymmetry: `GET .../core/suggestions-enabled` now 403s like `PUT`
  already did, rather than answering `true` for a toggle that can never exist (review of the
  agent's own instructions/playbooks is mandatory, ADR-0093). `core-app` 0.83.0→0.83.1 (PATCH).

- **Infra: the "docker socket unavailable" message overstated the impact, and the socket was
  mounted by default without ever actually working** (#622, ADR-0099). Module removal was never
  disabled — an earlier fix (ADR-0056) already made it tombstone the module immediately either
  way, deferring only the container teardown — but the log line (surfaced via the Observability
  console) still said "module removal disabled," and the socket mount in
  `services/core-app/compose.yaml` was unconditional despite never being reachable on a real
  deployment anyway (the core drops to an unprivileged uid at startup, ADR-0069, with no group
  matching the host's docker-socket GID). Now: the message says what's actually deferred; a new
  `GET /platform/v1/modules/docker-status` lets the Modules page state that proactively, with the
  one-line enablement, instead of an operator finding out by attempting a removal; and the socket
  mount is an explicit opt-in (`services/core-app/compose.docker-socket.yaml` + `DOCKER_GID`) —
  losing no real capability (nothing worked by default before either) while closing a
  root-equivalent attack surface that was pure liability. `core-app` 0.78.0→0.79.0; `web`
  0.108.0→0.109.0.

- **Mail: paging pinned to the bottom, action row at the top + icon-only on phone** (#624, #626) —
  two mailbox UX fixes. The **Newer/Older** paging controls scrolled away with the message rows
  (attached mid-content); each mailbox view now owns its scroll, so the paging bar is a stable
  footer pinned to the bottom of the list (#624). The per-message **action row** (mark read/unread,
  archive, trash) sat at the bottom of the message and is now anchored at the **top**, and on a
  narrow viewport the buttons are **icon-only** — labels hidden from `sm:` down, with the
  aria-label + tooltip preserved so they stay named and accessible (#626). `web` 0.107.0→0.108.0.

### Added

- **Chat: a live document pane — watch the agent write, then edit or review it in place** (#541,
  ADR-0101) — the agent writing a document showed only a `knowledge_create_doc ✓` chip; the
  artifact, usually the whole point of the turn, was invisible until you navigated to the module
  afterwards. Now an annotated call (the `writes_document` seam below) opens a **split pane beside
  the chat** — a resizable column on desktop, a sheet on phones — showing the document as it is
  written, read-only while the call is in flight so a user edit can't race the agent's.
  **What the pane becomes depends on what the write actually did.** Knowledge and notes *propose*
  documents rather than writing them (ADR-0033), and review defaults **on** — so with review on
  nothing was written, and the pane shows the proposal with **Review & approve** into that
  module's review page (where the draft is already editable, ADR-0090). With review **off** the
  change was applied, and the pane hands over to the **real editor archetype**: auto-save
  (ADR-0042), version history (ADR-0046), the same module document APIs, no second write path.
  The outcome is read from the core's own review setting — the same one the module consulted —
  rather than guessed, so the pane never claims a write landed when it didn't. Generic
  throughout: the module names the arguments and the shell reads the annotation (no module is
  named in web code, ADR-0018/0019), and the pane finds that module's editor/review pages from
  its own manifest, so a future adopter needs no web change. Best-effort by construction — a
  failed or slow lookup costs a pane, never the turn — and the body rides SSE only, never the
  persisted activity, so ADR-0041's caps are unchanged (the pane is a live-turn affordance; after
  a reload the turn's entity-ref chip is the way back, as it already is for un-annotated writes).
  `knowledge` and `notes` are the first adopters, on their four **full-body** write tools;
  `notes_append` is deliberately left out (its `text` is a fragment the server concatenates on
  approval, not a document). Also brings both modules' manifest versions back in line with their
  packages — they had drifted a minor behind, under-reporting on the Modules page. `core-app`
  0.82.0→0.83.0; `web` 0.110.0→0.111.0; `knowledge` 0.22.0→0.23.0; `notes` 0.7.0→0.8.0.

- **Modules: a `writes_document` tool annotation** (part of #541, ADR-0100) — the groundwork for
  the live document pane. A module declares, per tool, that it *writes a document* and names the
  arguments the document travels in — `writes_document: {content_arg, title_arg?, target_arg?}` on
  `ToolSpec`, declared through the usual decorator (`@module.tool(writes_document=…)`). The shell
  will use it to open the document beside the chat while the agent writes it, for **any** module,
  with no per-module web code (ADR-0018/0019) and no name-sniffing. It is an annotation, not a
  capability: the tool keeps its own name, schema, and behavior, gains no endpoint, and a tool that
  writes no document simply omits it. The named arguments are **validated against the tool's own
  input schema** at manifest-build time — a typo fails there instead of surfacing later as a pane
  that silently never fills — and declaring one for a tool that never registered is an error rather
  than a silent drop. Additive on the wire (`CONTRACT_VERSION` unmoved): a core predating the field
  ignores it, so a module can adopt it independently. No consumer yet — the pane, and knowledge +
  notes declaring the annotation, follow. `epicurus-core` 0.27.0→0.28.0.

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
  than a reference to a message that may no longer exist. `core-app` 0.81.0→0.82.0; `web`
  0.109.1→0.110.0.

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
