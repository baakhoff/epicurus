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
| `POST /platform/v1/agent/chat` | Run one turn (offer module tools → run tool calls over MCP → loop to an answer). The round bound is resolved **per turn** from the operator's stored pref, else the `AGENT_MAX_STEPS` env default (#297). The **model** is resolved per turn too (ADR-0113): the session's stored choice if it has one, else the request's `model` — so that field is the caller's default, not an override. Returns `AgentTurn`. |
| `POST /platform/v1/agent/chat/stream` | The same turn as **SSE**: an optional leading `readiness` (warming progress, ADR-0027) · `delta` (answer tokens) · `thinking` (chain-of-thought tokens, ADR-0041) · `doc_preview` (a slice of a document *as the model types it* — `text` carries a coalesced body delta and `preview` `{module, target?, title?}` names the document, #654/ADR-0121; purely ephemeral — see **The document typewriter** below) · `tool` (a tool ran — carrying `document` `{module, content, target, title}` when the module annotated that tool `writes_document`, so the shell can open the document pane, #541/ADR-0100/0101; on both the `running` and terminal frames, and never persisted into the turn's activity) · `awaiting_input` (the turn paused — for `ask_user` it carries `{run_id, question}`, ADR-0053; for a **draft-first send** it carries `{run_id, awaiting_kind: "draft_review", draft}`, ADR-0085/#563; for an **`ask_approval` pause** it carries `{run_id, awaiting_kind: "approval", summary, refs}`, #745/ADR-0117 — every shape additive, so a stale client ignores what it doesn't know) · `done` (final turn) · `error`. Each data frame carries an `id:` (a live-run seq) for re-attach. The turn runs **decoupled from this connection** (ADR-0055): a disconnect doesn't abort it — the answer still persists and the client re-attaches. A turn already running for the session yields **409** (+ `X-Run-Id`). The web shell speaks this. |
| `GET /platform/v1/agent/sessions` | List conversations (title + last-active + count), each enriched with its persisted **model override** (`model`; #707, null if never set — see `PUT .../model` below) alongside the existing automation badge/grouping fields. Either enrichment degrades independently on a lookup hiccup — the list itself is never emptied by one. **Invisible sessions are excluded** (#772), and every list read also runs the **orphan sweep**: any flagged session not named by the optional `?active=<session_id>` query param (the invisible chat the requesting client is currently *in*) and with no turn in flight is fully erased via the #771 cascade — so a crash never strands an invisible chat on disk. The sweep is best-effort; a hiccup never fails the list. |
| `PUT /platform/v1/agent/sessions/{id}/model` | An explicit picker change for **this** session (#707): `{model}` persists it, `{model: null}` clears the override (picking "core default" back). Writes the same field the `set_chat_model` tool does — the two paths share one owner of truth, whichever writes last stands. **400** on a blank (non-null) model; **503** if no model store is wired. Not validated against the model catalog — the picker only ever offers a real name, the same two sources (`GET /llm/models` + `GET /llm/saved-models`) the tool resolves against. |
| `PUT /platform/v1/agent/sessions/{id}/ephemeral` | Flag a session **invisible** (#772) — see *Invisible chats* below. Idempotent (a mid-chat reload re-marks so the flag is server truth, not client memory); **503** if no flag store is wired. Returns `{ephemeral: true}`. There is deliberately **no un-mark**: toggling invisibility off *is* an exit, and every exit deletes (`DELETE /sessions/{id}`). |
| `GET /platform/v1/agent/sessions/{id}` | A session's full transcript. Each message carries its **`id`** — the stable anchor a client names to edit that turn (`/edit`, #552) — alongside `role`, `content`, `created_at`, `entity_refs`, `attachments`, and (assistant turns) `activity`. |
| `GET /platform/v1/agent/sessions/{id}/active-run` | The session's in-flight run to re-attach to — `{run_id, last_seq}` or `null` if none is live (ADR-0055). How a client rediscovers a turn after a reload/reconnect. |
| `DELETE /platform/v1/agent/sessions/{id}/active-run` | Cancel the session's in-flight turn — the explicit **Stop** (a decoupled turn outlives the connection, so Stop must say so). Returns `{cancelled}` (ADR-0055). |
| `GET /platform/v1/agent/active-runs` | Session ids with an in-flight turn right now — `{session_ids}`. Drives the conversations-list running indicator (#396) in one request rather than polling each row; tenant-scoped, best-effort/point-in-time (the live-run buffer is a disposable cache). |
| `DELETE /platform/v1/agent/sessions/{id}` | Delete a conversation — **everything it produced, everywhere** (#771): its messages, its uploaded attachments' bytes (`agent_attachments`), its **still-queued extraction exchanges** (so the nightly drain can never distil a deleted chat), its model override (`session_models`), any suspended/pending paused runs (`agent_suspended_runs` / `agent_pending_drafts` / `agent_pending_approvals`), its automation badge mapping (`automation_sessions` — a rolling automation's next run simply re-records it), and any in-flight run's buffered state (the live turn is cancelled first). One tenant-scoped cascade; messages are deleted **last**, so a failure partway leaves the session visible and the whole operation retriable. Returns `{deleted}` (the message count — the pre-#771 field) plus additive per-store counts. **The boundary, stated honestly:** facts extracted on *previous* nights are curated memory, managed in the Memory view — deleting a chat stops all future derivation from it but does not un-learn them (ADR-0045); tool effects (a task created, a mail sent) and uploads already persisted to the storage sink's Files page belong to the modules that own them. The web confirm dialog says exactly this. |
| `POST /platform/v1/agent/sessions/{id}/regenerate` | Re-answer the session's last user turn, dropping the previous answer. Body `{model?}`. Truncates everything after the last user message, then streams a fresh turn — same SSE protocol as `/chat/stream`; an `error` event if there's no user turn (#302). |
| `POST /platform/v1/agent/sessions/{id}/edit` | Replace a user message with `{content}` (and `{model?}`, `{message_id?}`) and re-answer it in place — revises the message, truncates everything after it, then streams. **`message_id`** names the turn to revise (#552); omitted, it defaults to the session's **last** user message, so pre-#552 callers are unchanged. Editing further back discards the real turns behind it (the web shell confirms with the count first). Edits in place, never branching (#302). Every check runs **before** any write, so a rejected edit leaves history untouched — an `error` event on empty content, no user turn, a `message_id` that isn't a **user** message of **this** session, or a turn already running for the session (a live run's history isn't ours to cut; the `/chat/stream` **409** remains the backstop for one starting mid-flight). |
| `POST /platform/v1/agent/runs/{run_id}/resume` | Resume a turn paused by `ask_user`, supplying `{answer}` (ADR-0053). Consumes the suspended run, appends the answer as the pending tool call's result, and continues the same turn — same SSE protocol as `/chat/stream`. An `error` event if the run is unknown / expired / already answered. |
| `POST /platform/v1/agent/runs/{run_id}/draft` | **Confirm/Decline a draft-first send** (ADR-0085, #563). Body `{decision: "send" \| "decline", reason?}`. Consumes the pending draft; on `send` the core transmits it via the owning module's `POST /send` and appends the outcome (`Sent.` + id, or a relayed error hint) as the compose call's tool result, on `decline` it appends a "not sent" result (carrying any `reason`) — then continues the same turn (same SSE protocol as `/chat/stream`). An `error` event if the draft is unknown / expired / already resolved. Confirm/Decline is connection-gated client-side (#530); the `run_id` is the DB pause token, distinct from a live-run id. |
| `POST /platform/v1/agent/runs/{run_id}/approval` | **Approve/Reject an `ask_approval` pause** (#745, ADR-0117). Body `{decision: "approved" \| "rejected", comment?}`. Consumes the pending approval and appends the decision (+ any `comment`) as the `ask_approval` call's tool result, then continues the same turn (same SSE protocol as `/chat/stream`). Unlike `/draft`, this route never itself calls a module — by the time it's posted, the **web client** has already called the relevant module's own review-decision endpoint directly (the agent must never approve its own work; see the *Built-in agent tools* section below), so `comment` is purely informational, carried back to the model. An `error` event if the approval is unknown / expired / already resolved. |
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

**The module-facing chat path is gated the same way since #739.** `POST /platform/v1/chat`
had no vision check at all: the rule above guarded only the interactive agent turn, so a
*module* sending image content-parts to a text-only model hit exactly the silent-ignore /
provider-error pair the gate exists to prevent. `websearch`'s `link_ingest` (#739) is the
first module-initiated vision inference in the codebase, and it needs a refusal it can read.
The endpoint now runs the same `supports_vision` check before any provider call and answers
**400** with a structured detail — `{"error": "unsupported_media", "message": …, "model": …}`
— so a module can branch on `detail.error` and degrade honestly rather than parsing a
provider error string. Both `image_url` (OpenAI) and `image` (Anthropic-native) parts trigger
it, and an image anywhere in the history counts, not just in the last message; a text-only
request is untouched and pays no extra capability lookup. Shape and caller guidance:
[platform API](../reference/platform-api.md#sending-images-the-vision-gate-739).

**A PDF attachment gets a real reader, not a naive decode (#738).** Before this, every
non-image file — including a PDF — was decoded as UTF-8 (`errors="replace"`), which for a
PDF meant `[file: report.pdf]` followed by thousands of replacement characters: the same
noise images used to produce, just as text instead of an obviously-binary blob.
`agent/document_extraction.py`'s `extract_pdf` (pure-Python `pypdf`, no system deps) reads
the real text layer instead, page by page (`[page N]` markers), bounded to 20k characters
with a truncation note — larger than the plain-text excerpt cap (`_EXCERPT_CHARS`, 4k) since a
document attachment is the point of the turn, not incidental context. An encrypted PDF (beyond
an empty/owner-only password — the common "restricts editing, not reading" case) or a scanned
(image-only, no text layer) one renders an honest metadata block instead —
`[file: report.pdf — PDF, 12 pages, no extractable text]` — never mojibake, and the attachment
is never silently dropped either. The same honest-block treatment catches any *other* file
whose UTF-8 decode turns out mostly replacement characters (`is_mostly_binary`, a zip renamed
`.bin`) — no format gets to pretend binary is text. Out of scope, deliberately: OCR (a scanned
PDF reports "no extractable text", full stop), docx/pptx extractors (the seam is built to make
adding one trivial, not built yet), and native PDF pass-through for a hosted model that
accepts documents directly (extraction is the portable, local-first answer — Ollama can't take
a PDF at all; a capability-gated pass-through can layer on top of this seam later without
changing it). The platform read door (`GET /platform/v1/files/read`) is untouched — this seam
applies only at the attachment expander, the one place the mojibake actually reached the model.

**Tool results that carry entity refs also teach the model the ids** (ADR-0079). When a module
tool returns an envelope (`tool_envelope(text, [EntityRef…])`), the loop lifts the refs onto the
turn for UI chips — and appends a compact `title → id` listing to the tool result the **model**
sees, so a "list, then act on one" flow (list events → `calendar_update_event`) has a real id to
pass back. The block is model-only context: never rendered in chat, never part of the display
text. The module-author side of this contract is in
[the modules reference](../reference/modules.md).

**The same block also teaches the entity-link syntax** (#794). Each line additionally carries
the ready-made `epicurus://entity/{module}/{kind}/{ref_id}` link — every component
percent-encoded (`urllib.parse.quote(..., safe="")`) — and the intro tells the model: when you
*mention* one of these entities in your reply, link it inline as `[text](link)` using the URL
shown, verbatim, rather than building one by hand. The web shell already renders such a link as
an interactive chip and excludes it from the "Sources" pill (see
[web.md's entity-references section](web.md#entity-references-in-chat-adr-0019)) — that half of
the contract predates this change and was simply never exercised, because nothing told the model
the syntax existed. Percent-encoding every component (not just characters known to be unsafe
today) matters because the web's inline-link matcher stops at the first `)` or whitespace and
then `decodeURIComponent`s the captured id; an unencoded id containing either would silently
degrade to a dead link rather than error. The model is told to link only what it names, not
every ref — the pill stays the outlet for the long tail.

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

### Invisible chats (#772)

A session flagged via `PUT /sessions/{id}/ephemeral` is an **invisible chat**: everything works
normally while you're in it, and when you leave it is **deleted — not archived, deleted** (the
full #771 cascade). The design is *persist-flagged, then hard-delete*: the session writes
normally (so an accidental reload mid-conversation keeps the thread), and the flag row
(`ephemeral_sessions`) is what makes it different:

- **Hidden from every listing and learner while live.** Excluded from `GET /sessions` (and so
  from the web's sessions sheet), **never enqueued for fact extraction** — the agent checks the
  flag at learn time, in both `nightly` and `immediate` modes, failing *closed* (a flag-store
  hiccup skips learning rather than leaking; #771's session-stamped queue purge is the exit's
  backstop) — filtered out of the nightly reflection pass's transcript scan (transitively: the
  scan reads the store's `sessions()`, whose default now excludes flagged sessions), and out of
  `memory_search`'s past-conversation half, so an invisible chat can't surface inside another
  conversation. Profile synthesis reads facts, so it is covered transitively once extraction
  never sees the chat.
- **Every exit deletes.** Toggling off, switching sessions, starting a new chat, and closing
  the app all run `DELETE /sessions/{id}` from the client; the **orphan sweep** is the safety
  net — at startup (nothing can be live across a restart) and on every session-list read, any
  flagged session not named by the client's `?active=` and with no turn in flight is erased.
  One consequence, stated plainly: invisible chats are effectively **per-client** — another
  client's list read treats your idle invisible chat as stranded (an in-flight turn is spared).
  A core-app restart mid-invisible-chat likewise sweeps it (conservative by design: a crash
  must never strand one).
- **Honest semantics.** Invisible means the *transcript* evaporates — tool effects do not. A
  task created or a mail sent from an invisible chat persists, exactly like downloads from a
  private browser window; a file uploaded during it remains in Files (the storage-sink copy).
  Usage metering still records tokens — the core meters all inference (constraint #8) and the
  events carry no content. The per-session model picker works as usual; its `session_models`
  row dies with the session. Normal chats are byte-identically unaffected.

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

**The base system prompt is off-limits (#762, amending ADR-0093 §1–§3).** Reflection originally
offered two targets — a named playbook, or the base instructions themselves. The second target
no longer exists, for three reasons the feature's own shape makes acute: **(1) the pass reads
tainted input** — transcripts contain external content the agent quoted (mail bodies, web
results, document text), so planted "operator preferences" in something the agent merely *read*
could surface as a plausible edit to the very document the agent's rules live in; **(2) the
approval gate is weakest exactly where the stakes are highest** — an instructions proposal was a
full-document replacement, the largest possible diff, presented nightly, indefinitely, and
approval fatigue makes a one-line planted change in an otherwise-reasonable edit realistic to
wave through, after which it persists in the system prompt of every future turn; **(3)
governance asymmetry** — the governed system drafting revisions to its own governing document is
a conflict of interest even with review. Playbooks carry the same harvesting value with a
categorically smaller blast radius: additive, named, rendered under a visible `## Playbook:`
heading, individually disable-able, never rewriting operator-authored text. Enforced at **three
layers**: the reflection prompt no longer offers the target (the base prompt stays in its
context **read-only**, so proposals don't duplicate base rules, with an explicit "you may not
propose changes to it"); `_resolve` drops any `"instructions"` target the model returns anyway
(logged); and — defense in depth — the review sink refuses to stage a proposal whose path is the
instructions path **for every origin**, with the instructions apply-path removed outright, so no
future proposal source can quietly reintroduce the target. A pending instructions proposal that
exists at upgrade time still renders (diffed against the live base) and is cleanly
**rejectable**; Approve refuses it with a clear message. Operator editing of the base prompt via
**Settings** is unchanged — humans edit freely; this bounds the *agent's* proposal surface.

**The approval surface.** A proposal is an ordinary `ReviewSuggestion`
([`epicurus_core.review`](../reference/platform-api.md), ADR-0090) — `operation: "update"`
against an existing playbook, `"create"` for a new one — so the existing
`ReviewView` / `SuggestionReviewModal` render it with the same diff, editable draft, and audit
trail every module's queue gets. Approve applies the (possibly hand-edited) content through the
playbook store below; reject discards. Both record a durable decision row.

**The reserved `core` pseudo-module.** Every other `review`-page implementer is an external
module the registry reaches over HTTP; the core hosts no page of its own. Rather than bend the
core into a module that calls itself over the network (rejected in the ADR as needless
indirection), `ModuleRegistry` accepts one **reserved entry named `core`** that it answers
**in-process** — see *Module registry* below. It rides `GET /platform/v1/modules` so the shell
discovers its page like any module's, with no new endpoint and no new frontend contract.

**Where proposals come from.** A `playbook-reflection` job on the nightly maintenance batch (see
*Maintenance orchestrator* below — registered `nightly=True`, so it rides the orchestrator's one
schedule rather than an *hour* knob of its own). Per tenant it scans the sessions active since
its last run — **invisible sessions excluded** (#772) — and makes **one** gateway call over
them, asking for candidate playbook edits; each is staged for review. It is metered under **the
tenant whose sessions it scanned** — never a synthetic background tenant (constraints #1/#8,
the ADR-0051 drain's precedent). Since #762 the pass has its **own off-switch**,
`PLAYBOOK_REFLECTION_ENABLED` (default on, now that it is playbook-only): disabled, the nightly
job reports `skipped — disabled (PLAYBOOK_REFLECTION_ENABLED=false)` **without spending a
gateway call** — previously the only way to stop reflection was disabling all nightly
maintenance (fact extraction, profile synthesis, re-index included). Details that matter:

- **It cannot apply anything.** It is handed a proposal sink and a *read-only* playbook lookup,
  never the stores that own the documents — the ADR's hard rule is enforced by construction, not
  by discipline.
- **The operation is derived, not trusted.** A playbook the tenant already has is an `update`,
  otherwise a `create` — decided from what exists, because a mislabelled `create` would render an
  empty *current* side and hide from the operator exactly what their approval would overwrite.
- **Rejections feed back.** Recently rejected proposals are digested into the prompt as explicit
  negative context (from the `agent_playbook_decisions` trail, ADR-0093 §6), so a declined idea
  isn't re-proposed unchanged.
- **It doesn't stack drafts.** A document with a proposal still awaiting the operator is skipped,
  so the queue can't grow while they're away.
- **A watermark bounds the scan** (`agent_reflection_state`, durable — an in-memory marker would
  reset on restart and re-propose the whole history). It is snapshotted *before* the scan and
  advanced only on a completed pass, so a session written mid-pass is re-read next time rather
  than lost; re-reading is harmless (a duplicate is suppressed), losing one is not.
- **Junk costs nothing.** A non-JSON reply, an unknown target, a nameless playbook, or a runaway
  generation stages nothing rather than raising. Nothing new since the last pass? No gateway call
  at all.

**Storage and undo.** An approved playbook edit writes through the playbook store; the base
prompt is written **only** by the operator's own Settings edit (`instructions_routes.py`) —
since #762 no proposal path reaches `AgentInstructionsStore` at all. Both stores version
ADR-0046-style (snapshot-on-save, capped at the same `MAX_VERSIONS = 50`, oldest pruned). One
deliberate departure from the editor's version store: it snapshots the content *being saved*;
these snapshot the content being **replaced**. The editor accumulates many operator saves, so
the prior body is always somewhere in its history; here the very first write may be an approved
agent-authored edit against a body never saved through this path, and recording only the new
content would leave the original unrecoverable — exactly the undo the ADR says an agent-proposed
edit needs. A save that changes nothing records no version.

### Governed automations (#667, ADR-0107)

The core hosts a **second in-process review page** beside playbooks: staged automations. The
`propose_automation` built-in (below) drafts an automation from a chat request and stages it here;
the operator reviews it — trigger in words, filter, action, autonomy, sinks, and an editable
**model picker** — and **approves** (which creates the automation *enabled*; approval is the
consent) or **rejects** (audit trail only — the `#687` suggestion-decision events fire at that
seam). The tool never creates or enables anything itself; only an approval does.

Both pages ride the one reserved `core` pseudo-module (ADR-0093 §2): a small `CorePages` composite
(`core_review.py`) declares both `PageSpec`s and dispatches `get_page` / `review_action` /
`review_audit` by `page_id`, so the `ModuleRegistry` — which already fans out over a manifest's
pages — needs no change. This is one more page in the single Suggestions inbox, **not** a second
review surface: both render through the same unmodified `ReviewView` / `SuggestionReviewModal`. The
shared suggestion contract carries a small additive `automation` field
(`epicurus_core.review.AutomationPreview`) so the modal renders the automation understandably
rather than as a raw text diff; an `update` proposal also carries a readable before→after diff, and
its approve `content` is the operator's chosen model (`""` = the tenant default). The staged
proposals (`automation_proposals`) and their decision trail (`automation_review_decisions`) mirror
the ADR-0090 storage shape.

### Built-in agent tools (ADR-0039)

Besides the modules' MCP tools, the core offers **built-in tools** the agent can call,
dispatched in-process (no module round-trip). They're registered on the `McpHost`
(`register_builtin`) and routed via a `"__builtin__"` sentinel; they respect the same
per-tool disable filter as module tools.

- **`now(timezone?)`** — the current date/time. The agent has no inherent clock, so it
  calls this for anything date/time-relative ("tomorrow", "at 19:00", a bare weekday name
  like "monday"). Returns the time in the operator's configured timezone (or the
  `timezone` argument) plus UTC and the weekday; when a connected calendar uses a
  *different* timezone, that is reported with a note so events land in the intended zone.
  The payload also resolves bare weekday names to dates (#793), so the model never does
  the arithmetic itself: `today`, `tomorrow`, and `upcoming` — a map from each weekday
  name to its next date **strictly in the future** (asked on a Friday, `upcoming.Friday`
  is next Friday, not today) — all computed from the same zone-resolved instant as the
  rest of the payload, so the operator's zone (not UTC) decides what "today" is, the same
  care #559 took for calendar read paths. The tool description tells the model to use
  `upcoming` verbatim and to `ask_user` rather than guess when phrasing like "next Monday"
  is genuinely ambiguous. The configured timezone is read from:

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
- **`propose_automation(name, action, autonomy, …)`** — draft an automation from the user's
  natural-language ask and **stage it for approval** (#667, ADR-0107). The conversational front
  door to the automations engine: "when I get mail from my boss, notify me", "every Monday at 9am
  summarize last week" becomes one drafted spec per call (call it twice for two pipelines), staged
  as a `ReviewSuggestion` on the core **automations review page** (see *Governed automations*
  below). Classified `propose` — it stages for approval by construction, like `knowledge_propose_*`
  — so it is offered in ordinary chat and withheld from a Notify automation. The **hard guardrail**:
  the tool can *only* stage; it has no path to create or enable an automation at any autonomy level.
  Approving the suggestion is the one path that creates one, and it creates it enabled.
- **`ask_user(question)`** — pause the turn to ask the operator a clarifying question
  (ADR-0053). Unlike other built-ins it is **not executed inline**: the agent loop intercepts
  the call, persists the in-progress run (`agent_suspended_runs`), emits an `awaiting_input`
  SSE event, and ends the stream. The web shows the question + an input; the answer is POSTed
  to `…/agent/runs/{run_id}/resume`, which rehydrates the run and continues the same turn with
  the answer as the tool result. The suspended run is consumed on resume and reaped after
  `ASK_USER_TTL_HOURS`. With no suspend store wired the loop degrades — the model is told to
  proceed with its best assumption rather than pausing.
- **`set_chat_model(model)`** — switch the *current session's* model, from the next reply
  onward, and remember the choice (#707). "Answer with grok from now on" resolves `model`
  against exactly what the web's picker offers — installed local models
  (`LlmGateway.models`, hidden ones excluded) and the tenant's saved hosted models
  (`SavedHostedModelStore`) — case-insensitively, exact match first, then a substring match
  but *only* when it's unique; an unknown or ambiguous name changes nothing and returns the
  available list rather than guess. Persists to `SessionModelStore` (a small sidecar table —
  there is no other "session row" to put it on; a session is derived from `agent_messages`
  via `GROUP BY`, see **Data model**), keyed by `session_id`, so it survives a reload and the
  picker reads it back (`GET /sessions` enriches each summary's `model`). An explicit picker
  change writes the *same* field via `PUT /sessions/{id}/model` (`model: null` clears the
  override back to the device default) — one owner of truth, so a tool switch and a manual
  pick can never fight, whichever happens last simply stands. **The core resolves that row at
  turn time** (ADR-0113): `/chat`, `/chat/stream`, `/sessions/{id}/regenerate` and
  `/sessions/{id}/edit` each read it before starting the turn, so `model` on the request body
  is the *caller's default* — used only while the conversation has no choice of its own — and a
  session that has been given a model runs it whatever the caller sends. Resolving server-side
  rather than in the client is what makes the tool trustworthy: a client computing this reads a
  cached sessions list, so a turn sent before that cache refreshed would silently run the
  previous model immediately after the agent said it had switched. A store failure degrades to
  the caller's default rather than costing the turn. Requires an active session:
  with none (e.g. some headless path) it errors plainly rather than silently doing nothing.
  Classified `write` — it follows the ordinary autonomy dial exactly like `remember`/
  `ask_user` rather than a bespoke automation exclusion, and is naturally inert for the
  common automation case anyway (no chat sink ⇒ no session ⇒ the handler errors). Changing
  the tenant-wide default model or a module's model slot stays in Settings — a mid-chat
  remark never rewires more than the one conversation.

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

**`ask_approval(summary, refs)`** pauses the turn for the operator to Approve/Reject a change the
model just staged through a propose tool (#745, ADR-0117) — a third sibling of `ask_user` and
draft-first-send, on the same suspend/resume discipline: the loop persists the run plus `summary`
and `refs` (the entity reference(s) the propose tool's result gave the model for what it staged,
copied verbatim — possibly empty) to `agent_pending_approvals` (a sibling of
`agent_suspended_runs`/`agent_pending_drafts`, reaped after `ASK_APPROVAL_TTL_HOURS`) and emits
`awaiting_input` with `awaiting_kind: "approval"`. It differs from both siblings in one deliberate
way: resolving it never calls a module from the core. The chat UI renders an approval card from
`summary`/`refs`; **Approve**/**Reject** first call the relevant module's own existing
review-decision endpoint directly — the operator's own click drives it, never the agent, which is
exactly the boundary knowledge's suggestion review already draws (`suggestions.py`: "the agent
must never approve its own work") — and only then POST `{decision, comment?}` to
`…/runs/{run_id}/approval`, which appends the outcome (plus any `comment`) as the tool result and
resumes. A module call failure doesn't block the resume; it is folded into `comment` instead, so
the model is told plainly rather than assuming the change took effect.

**Not on this list: `finish_quiet` and `ask_approval`.** Neither is registered via
`register_builtin` — those tools are offered to *every* turn (filtered only by the autonomy dial,
which an ordinary chat turn bypasses entirely), and both must be withheld from turns where they
don't apply. `finish_quiet` is spliced into `Agent._loop`'s tool surface directly, gated on
`automation_id`/`quiet_capable` — see *Automations engine → Agent-gated delivery* below.
`ask_approval` is spliced into `run_stream`'s tool surface directly, unconditionally: `run_stream`
carries no `automation_id` concept at all, so the tool is inherently absent from every headless
path (`_loop` — automations, scheduled turns, inbound-bridge replies) rather than needing a gate of
its own — those callers keep today's async review-queue behavior unchanged.

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
| `GET /platform/v1/llm/providers` | Providers and what the secret store knows about each one's key. Each row is `{alias, local, configured, needs_base_url, key_state, key_error}`. `key_state` is `not_required` (the local runtime holds no key) / `present` / `missing` (OpenBao answered and has nothing there) / `unavailable` (OpenBao could not be asked — an expired app token, the service down), with `key_error` naming the reason for the last one. `configured` is unchanged (`true` for `not_required` and `present`) — it was one bit over three facts, and collapsing "we could not ask" into "there is no key" is how #728's expired token read as a fleet of unconfigured providers, sending the operator to re-enter keys that were already set. The core reports the distinction; rendering it is the shell's job (ADR-0018) and is not wired up yet — nothing in `services/web` reads `key_state` today. |
| `PUT` · `DELETE /platform/v1/llm/providers/{alias}/key` | Store / clear a hosted provider's key (core → OpenBao; never logged or returned). |
| `GET /platform/v1/llm/prefs` | Stored preferences: `global_default` (chat), `global_embed_default` (embedding), `global_context_window` (num_ctx), `kv_cache_type` (Ollama KV-cache), `global_agent_max_steps` (agent loop bound), `hidden` (model list). |
| `PUT /platform/v1/llm/prefs/default` | Set or clear the global default chat model (`{model: str|null}`). |
| `PUT /platform/v1/llm/prefs/embed-default` | Set or clear the global default embedding model (`{model: str|null}`). Modules with no per-module override use this; per-module selections win (#214). |
| `PUT /platform/v1/llm/prefs/context-window` | Set or clear the **global** Ollama context window (`{value: int|null}`); the default for models without their own setting. |
| `PUT /platform/v1/llm/prefs/kv-cache-type` | Set or clear the operator's preferred Ollama **KV-cache type** (`{value: "q8_0"\|"q4_0"\|null}`, `null` = the f16 default). Server-wide; persisted, then **applied**: the core writes Ollama's start-up env file (enabling flash attention for the quantized types) and restarts the container (#307, amends ADR-0046). Returns `{value, applied, staged}` (#709) — **two** flags because there are two degraded modes. `applied` = the running server has the new value. `staged` = the env file holds it and only a container restart is missing (the usual case without Docker access: the entrypoint re-sources the file on every start, so `docker compose restart ollama` applies it and **no environment editing is needed**). `applied` implies `staged`. Only `staged: false` — the file could not be written at all — calls for setting `OLLAMA_KV_CACHE_TYPE`/`OLLAMA_FLASH_ATTENTION` by hand, which is what the UI used to say in every degraded case. Clearing back to the default stages identically (a successful unlink is the choice on disk). |
| `PUT /platform/v1/llm/prefs/agent-max-steps` | Set or clear the agent loop bound — tool-calling rounds per turn (`{value: int|null}`, clamped 1-12; `null` = the `AGENT_MAX_STEPS` env default). Resolved per turn, no restart (#297). |
| `PUT /platform/v1/llm/prefs/hidden` | Toggle a model's hidden state (`{name, hidden}`). |
| `GET /platform/v1/llm/saved-models` · `POST` · `DELETE ?model=…` · `PUT …/capabilities` | The tenant's **saved hosted-model ids** (#496). `GET` → `{models:[{model, provider, context_length, capabilities, override}]}` (most-recent-first) — `context_length`/`capabilities` (#618) come from the same LiteLLM model-cost lookup as `/models/details`, always included (a static lookup, not a network call, so unlike the local list this isn't gated behind an opt-in query param); `null`/empty when the model isn't in LiteLLM's map. `POST {model}` persists one, idempotent — an atomic upsert (**400** if it isn't a hosted `<provider>/<model>` id, so a local `hf.co/…` **or** a provider-only `claude/` with no model can't land). `DELETE ?model=…` forgets one (removing the id that is the current global default leaves `llm_prefs.global_default` pointing at it — still valid for inference, just unlisted). Backs the chat picker (auto-saved on use), the Models page (remove / set-as-default / edit capabilities), and module model slots; persisted in `saved_models`. `PUT …/capabilities {model, vision, context_length}` sets the **capability override** (#711) — see *Capability resolution* below; **404** for an id the tenant hasn't saved. Mutations **503** without the store. |
| `GET /platform/v1/llm/model-settings?model=…` · `PUT /platform/v1/llm/model-settings` | Per-model tuning (context window, keep-alive, device) for one model, chat **or** embedding. `GET` returns `{context_window, keep_alive, device}` (each `null` = inherit; `device` is `"gpu"`/`"cpu"`/`null`=auto); `PUT` body `{model, context_window, keep_alive, device}` (an all-`null` body clears the override). Works for a **hosted** `<provider>/<model>` id too — there `context_window` is a **compaction budget** (`keep_alive`/`device` are local-only Ollama options). Persisted in Postgres (`model_settings`). See **Per-model settings** below. |
| `POST /platform/v1/llm/model-settings/suggest-context` | Compute **and persist** a recommended per-model context window for a freshly pulled model (#386), so it opens sized to itself instead of the global default. Body `{model}`. Reuses the `system/info` heuristic (VRAM-or-RAM + the named model's on-disk size + KV-cache type, capped at its trained length) but for *that* model rather than the active one. **Non-destructive** — an existing per-model context override is left untouched. Returns `{model, context_window, applied}` (`applied` is `false` when one was already set, or none could be computed — e.g. a hosted model with no local size). The web calls it when **any** pull finishes (catalog, variant, or manual tag). |
| `GET /platform/v1/system/info` | Host spec + the context-window suggestion behind the Models page. Returns `{gpu, cpu, ram_total_mb, model:{name, size_mb, context_length, quantization}, suggested_context:{min, suggested, max}, kv_cache_type}`. The suggestion estimates how big a context the box can hold from VRAM (or RAM, no GPU), the active model's on-disk size, and the **KV-cache type** (a quantized cache `q8_0`/`q4_0` costs fewer bytes/token, so the same memory buys more context). Its ceiling is the model's **trained** `context_length` when known — no longer a flat 32k — so a long-context model on a roomy GPU is no longer clipped; 32768 remains only the fallback when the trained length is unknown. Best-effort: every probe degrades to `null`. |

#### Capability resolution (#633, #618, #711)

Two questions get asked about every model: **can it see images** (`supports_vision`, which gates
an image attachment) and **how much context does it have** (a badge, and the ceiling on the
context-window suggestion). They resolve in this order:

1. **The operator's per-saved-model override**, when one is set (#711).
2. **The local runtime's `/api/show`** for a local model — an explicit `vision` capability says
   yes, anything else (including an unreported list on an older Ollama) says no.
3. **LiteLLM's static model-cost map** for a hosted model.

Step 1 exists because step 3 is a *curated static list* while model ids are the operator's choice
(ADR-0010) — the two are guaranteed to drift. The map omits ids entirely (`grok/grok-latest`
resolves to an unmapped `xai/grok-latest`) and mislabels others, and the failure is not cosmetic:
`supports_vision()` resolves `False` and the image gate refuses image turns for a model that
would have handled them. Renaming the saved model to a mapped id was the only workaround, which
is not the operator's job.

The override is `{vision: "auto"|"on"|"off", context_length: int|null}`, stored in two nullable
columns on the model's `saved_models` row and edited in the Models page's hosted-model sheet.
`auto` with no context length is the pre-override behaviour exactly, so an absent or cleared
override changes nothing. It applies **even when the map lookup raises** — an unmapped id is
precisely the case it exists for, so that path must not be the one that skips it.

Two boundaries worth keeping straight:

- **The override's `context_length` is not `ModelSettings.context_window`.** The first is *what
  the model has* (metadata, a badge); the second is *how much of it we choose to send* (a
  compaction budget, #570). Same word, different layer — both appear in the same sheet.
- **Gating and display only.** Routing, provider keys, and usage metering never consult the
  override; every model concern still lives in the core (constraint #8).

A miss against the map logs **once per model id per process**, then at debug: a saved alias
outside a curated list is expected, not anomalous, but the first sighting still explains a model
that shows no badges.

#### First-boot model bootstrap (#773, ADR-0118)

A fresh install boots an **empty Ollama volume** (models are never baked into the image), so
the first chat or embedding call would 404 until someone found the Models page — and
background work (the knowledge indexer, memory recall) failed noisily meanwhile. On startup
the core now ensures the deployment's default local models exist: a fire-and-forget lifespan
task (`llm/bootstrap.py`) waits for the runtime, resolves the **effective** chat + embedding
defaults (stored prefs, else `LLM_DEFAULT_MODEL` / `MEMORY_EMBED_MODEL`), and pulls the
missing ones through the same `gateway.pull()` path the Models page uses — then applies the
same post-pull context suggestion (#386), so a bootstrapped model opens correctly sized too.

Behaviour is bounded and defensive, in keeping with what startup may cost:

- **Never blocks** startup, readiness (ADR-0027), or a live turn — the pull happens in the
  background while the rest of the core serves.
- **Retries with exponential backoff** per model (the pull resumes partial downloads), then
  gives up with a warning naming the Models page as the manual fallback.
- **Hosted ids are skipped** (`claude/…` cannot be pulled into the local runtime), and an
  unreachable runtime (a hosted-only deployment running no Ollama) costs one warning after a
  bounded wait, never a crash loop.
- An already-provisioned deployment no-ops after one `/api/tags` round trip.

`LLM_BOOTSTRAP_MODELS` tunes it: `auto` (default) resolves the effective defaults; blank
disables the bootstrap (air-gapped builds — and the CI smoke gate, which must not download
multi-GB weights); an explicit comma-separated list pulls exactly those.

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

##### What the parser keys on (#710)

The index is HTML, so the selectors are the fragile part — and in 2026 they broke: the page
dropped the `x-test-*` attributes the parser had keyed on, every refresh parsed to `[]`, and
the box served the seed for weeks behind a warning repeated every refresh interval. The
selectors are now chosen for **survivability**, in this order of preference:

| Signal | Anchor | Why it was chosen |
| --- | --- | --- |
| Model block | the per-model `<a href="/library/<name>">` element | the link *is* the product; a redesign that removes it removes the page |
| Name | that same `href` | one source of truth, no title/heading fallback to drift |
| Description | the first `<p>` in the block that is **not** the stats line | structural, so a blurb that merely says "updated"/"tags"/"pulls" is kept |
| Stats line | a `<p>` whose `Pulls`/`Tags`/`Updated` labels are *whole elements* | the labels are user-visible copy, not styling |
| Pull count | the last count element before the word `Pulls` | ditto — anchored on copy, tolerant of markup between |
| Chips (capability / size / cloud) | any rounded **badge** span, classified by its **text** | see below |

The chip rule is the load-bearing one. Upstream distinguishes the three chip kinds only by
Tailwind colour (`bg-indigo-50` capabilities, `bg-[#ddf4ff]` sizes, `bg-cyan-50` cloud), and
keying on colour would mean a palette change silently reads sizes as capabilities. Classifying
by text instead — a chip matching `^(?:e|\d+x)?\d[\d.]*[bm]$` is a size, anything else is a
capability — partitions all 233 live blocks exactly, and a restyle degrades to "no chips
parsed" rather than to wrong data. The size pattern deliberately admits **mixture-of-experts**
(`8x7b`, `128x17b`) and **"effective"** (`e2b`, `e4b`) labels: each is a real pullable ref, so
a stricter pattern drops the entry. A capability outside the tag vocabulary (`audio` is live
today) is simply ignored.

`tests/fixtures/ollama-library.html` is a trimmed **verbatim** capture of the live index; the
tests assert against it so the next redesign fails CI instead of the running box. Regenerate it
from a fresh capture of the same families rather than hand-editing it.

**Bounded failure logging** (#710): a persistently broken upstream must not write a warning
every `LLM_CATALOG_REFRESH_SECONDS` indefinitely. The first failure of a streak logs at
**warn**, a *changed* error message logs at warn again (a new symptom is news), and the rest of
the streak logs at **debug** with a running `failures` count. The recovering refresh carries a
`recovered_after` field, so the end of a debug-quiet outage is still visible at info.

**Cloud-only models** (#571): some upstream families publish no downloadable weights at all —
their only tag is a `cloud` alias whose inference runs on the library vendor's cloud. The
index marks them with a `cloud` chip, which the parser reads like any other chip — by its text.
The parser adds `cloud` to the tag vocabulary (alongside the `thinking` capability, new in the
same pass) — but only on a family's **size-less bare entry**: hybrid families carry the chip
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

The tags-page selectors came through the redesign that broke the index parser **unchanged**
(re-verified 2026-07-25, #710): they key on the `/library/<family>:<tag>` link and on the size
string in the row's own text, neither of which the restyle touched. They are pinned the same
way regardless — `tests/fixtures/ollama-tags-llama3.1.html` (sizes and quants) and
`ollama-tags-glm-5.1.html` (a cloud-only family, whose row publishes no size).

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

Since #654 the assembly is also **observable while it happens**: each arriving fragment is
surfaced as a `StreamEvent.tool_call` — `ToolCallFragment{slot, id?, name?, arguments?}` — where
`slot` is the accumulator's own slot (so a consumer inherits the index discipline above rather
than re-deriving it), `id`/`name` are the call's values *as resolved so far* (a continuation
fragment still names its tool), and `arguments` is strictly the delta this fragment added, or
`None` for the whole-dict provider flavour (which is replaced rather than appended, so there is
nothing incremental to report). Purely additive: the accumulation and the final `result` are
untouched, and a consumer that ignores the field behaves exactly as before.

### The document typewriter (#654, ADR-0121)

The document pane (ADR-0101) opens when an annotated `writes_document` call *lands*. The
typewriter shows the body arriving instead, off the streamed fragments above:

- **`DocumentPreviewTracker`** (`agent/doc_preview.py`) resolves each call's tool name once
  against the registry's `writes_document` annotation and ignores everything else.
- **`StreamingArguments`** (`agent/partial_json.py`) is a resumable scanner that decodes one
  named top-level string argument out of JSON that is still an unterminated fragment. It is
  split-invariant: a `\` cut from what it escapes, a `\uXXXX` cut from its digits, or a surrogate
  pair cut in half is held back until it is complete, so a consumer never sees a half-escape or a
  lone surrogate (which would fail to encode into the SSE frame that carries it). Malformed JSON
  marks the reader broken and keeps what it had — a preview is never worth an exception. Only the
  body is shown half-written; `target_arg`/`title_arg` are reported only once *closed*, so the
  pane's header fills in rather than flickering through a half-typed title.
- **Throttle.** A `doc_preview` frame is emitted at most every `PREVIEW_INTERVAL_S` (**0.1 s**)
  per call, or sooner once `PREVIEW_MAX_CHARS` (**4096**) have piled up — the first slice goes out
  immediately, and the tracker is flushed when the gateway stream ends so the tail is never
  withheld. The interval bounds the frame *rate* (~10/s whatever the token rate) and the cap
  bounds frame *size*; together they are #541's answer to "a large document must never starve the
  chat deltas", which share the one stream.
- **Ephemeral (ADR-0041).** A preview never touches the turn's timeline — not `append_tool`, not
  `activity` — for the same reason v1's finished `document` payload doesn't, only more so: it
  reads an *unfinished* call. The persisted step is unchanged: one entry, with the pre-existing
  capped `detail`.
- **Hand-off (ADR-0101).** The `tool` frame that follows carries the arguments as actually
  *parsed* and replaces whatever the typewriter drew, so the pane can never keep showing a guess
  after the real thing is known. The pane is read-only from the first previewed character through
  the settle, which is what keeps #541's edit/write conflict structurally impossible.
- **Re-attach.** Previews ride the same seq-tagged live-run buffer as every other frame, so
  `GET /runs/{id}/stream?after_seq=N` replays them in order: from `0` (a reload) the pane rebuilds
  the body from its deltas, from `N` it continues. No snapshot is held or re-sent — that would
  make the run carry document state it has no other reason to keep, and would hand a resuming
  client a prefix it already has.

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

> **Privileged surface, least-privilege by default (ADR-0028, #307, #382, #622/ADR-0099,
> #708/ADR-0109).** Tearing down a removed module's container — and applying the Ollama
> KV-cache type — needs to reach Docker. The core touches it through a single
> `DockerController`: it stops/removes **only a configured module's own container**, and
> separately **restarts only an allowlisted infra container** (`ollama`, which is never
> removable). Both are scoped to this Compose project and never touch core-app / web / a
> data-plane service. By default this goes over `DOCKER_HOST=tcp://docker-proxy-core:2375` — a
> filtered proxy allowlisting exactly those calls and refusing exec/create/attach/images/
> volumes/networks/system before they reach the socket at all; `services/core-app/compose.
> docker-socket.yaml` opts into the raw socket instead (mounts it **and** forwards `DOCKER_GID`,
> the host's docker-socket group id, since the app's unprivileged uid — 10001, the same
> [entrypoint privilege drop](../infrastructure/index.md#shared-file-space) the shared file
> space uses — can't reach a direct mount without a host-matched group). Either way, module
> **removal itself never needs Docker to be reachable** (#382): it tombstones the module (hidden
> + unrouted at once) regardless, and **defers** the container teardown to the next startup
> reconcile when neither path is reachable — so removal always works; a KV-cache change
> likewise saves without applying. See [Docker-socket
> access](../infrastructure/index.md#docker-socket-access-708-adr-0109). `GET
> /platform/v1/modules/docker-status` reports the live state so the Modules page states it
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
guarantee (#472). Its dispatch runs over MCP rather than a plain HTTP proxy — the mcp 2.x
`Client` (one object owning transport + session + handshake; per-operation connections as
before) — so the host (`McpHost.call`) bounds every hop: the RPC rounds (handshake and the
tool call) with a 30s session-level read timeout, the connect phase by the SDK HTTP
client's 30s connect default. It normalizes a refused/dropped connection or an RPC read
timeout (which the client's anyio task group can raise **wrapped in an
`ExceptionGroup`**) into a single `ModuleUnreachableError`. `ModuleRegistry.invoke` maps
that to the **502** above; a tool that *ran* and reported failure stays a **400** with its
own message (`ToolCallError`, #435). The two are kept distinct on purpose — "the module
never answered" vs. "the tool rejected the request".

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
registers by being added to the list; the run / route / schedule machinery is unchanged. Six
ship: the **memory fact-extraction drain** (light, nightly-eligible — drains the
deferred-extraction queue, ADR-0051), the **standing-profile synthesis** (light, nightly-eligible
— `ProfileSynthesizer.run` distils each tenant's facts into its statically-injected profile,
ADR-0094), the **playbook reflection** pass (light, nightly-eligible — `PlaybookReflector.run`
proposes edits to the agent's own guidance for the operator to approve, ADR-0093; see *Governed
playbooks* above), the **module re-index** fan-out (heavy, manual-only — the same `reembed`
fan-out as above), **memory facts re-embed** (heavy, manual-only — calls
`UserFactStore.reembed_all` for the default tenant, #436), and the **run-history prune** (light,
nightly-eligible — trims its own persisted history, below, #733). Jobs run **sequenced** (gentle on a
single GPU) and each is contained:
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

**Persisted run history (#733).** `maintenance_history.py`'s `MaintenanceRunStore` — a
tenant-scoped `maintenance_runs` table — is the durable counterpart to the orchestrator's own
in-memory `last_run`, which a restart erases. The orchestrator takes an `on_recorded` callback
(mirrors `AutomationRunner.on_recorded` → the live runs feed) invoked with every completed run,
success or per-job error alike; `app.py` wires `on_recorded=maintenance_history.record`, so the
orchestrator itself never imports the store. A **shutdown-interrupted** batch is still recorded —
just from `MaintenanceOrchestrator.shutdown()` rather than from `_drive`'s own
`except CancelledError`, which cannot safely `await` again once caught (see its docstring) — a
crashed batch leaves a row, not a mystery, even though (like today) it publishes no
`maintenance.completed` event. Every run also carries a `source` (`"scheduled"` | `"manual"`,
defaulting to `"manual"` — the only two real callers, `_tick` and `POST /run`, always pass it
explicitly) that the old in-memory `last_run` never distinguished. Retention is both a row cap
and an age cutoff (`MAINTENANCE_RUN_HISTORY_MAX_ROWS`/`_MAX_AGE_DAYS`, default 200/90) — pruned by
the run-history-prune job above, the same "count or age" posture the module-event log and
notification center each apply to their own retention question.

| Method · Path | Purpose |
| --- | --- |
| `GET /platform/v1/maintenance` | `{schedule_enabled, schedule_cadence, schedule_hour, schedule_weekday, next_run_at, jobs:[{key,label,nightly}], last_run, current_run}` — the registered jobs, the *effective* schedule (the tenant's own override, else the env-configured default), an ISO `next_run_at` estimate (`null` when disabled — a display estimate only; the scheduler's own due-check additionally avoids re-firing within an already-run window), the newest persisted history row (`last_run` — reads the store, survives a restart; `null` before any run has completed), and the in-flight run (or `null`) with its live per-job progress. |
| `GET /platform/v1/maintenance/runs` | Persisted history, newest-first — `{runs:[{id,started_at,finished_at,scope,source,jobs:[{key,label,status,detail}]}], next_cursor}`. Query params `cursor` (a prior page's `next_cursor`; omit for the newest page) and `limit` (1-200, default 50). `next_cursor` is `null` past the last page. |
| `PUT /platform/v1/maintenance/schedule` | Set the tenant's schedule — body `{enabled, cadence: "hourly"\|"daily"\|"weekly", hour: 0-23, weekday: 0-6\|null}` (#621). Validated as a whole (**400** on an invalid shape — an unknown cadence, an out-of-range hour, a `weekly` with no/bad weekday, or a weekday given for a non-weekly cadence) before it persists; returns the full refreshed `GET` shape. |
| `POST /platform/v1/maintenance/run` | **202** — starts every job now (`scope: "all"`, `source: "manual"`) as a background task and returns its live progress immediately: `MaintenanceCurrentRun` `{started_at, scope, jobs:[{key,label,status,detail}]}` (`status` ∈ `pending`/`running`/`ok`/`skipped`/`error`). **409** if a batch is already running — the body is a plain `{detail}` message; re-`GET` for the in-flight run. |

The **manual** trigger (the web **Settings → Maintenance** card) is always available and runs all
jobs regardless of the schedule; the card rehydrates onto `current_run` on mount and polls a few
seconds apart while one is live, so a page refresh mid-batch lands back on the same run instead of
losing it. A **Run history** list underneath pages back through the persisted history.

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
run publishes a tenant-scoped `maintenance.completed` **and** is persisted to history; a run
interrupted by shutdown is recorded to history too (#733) but publishes no event, same as before.

### Scheduled turns (ADR-0092)

Recurring prompts that run **unattended** and deliver into their own chat session — the
time-driven half of proactivity (the event-driven half, listeners/alerts, shipped
alongside it inside 1.0 — see the [Automations engine](#automations-engine-adr-0105) below,
#662–#672). An operator authors a prompt, a cadence (daily/weekly at a local hour), and it
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

### Module event spine — durable intake (ADR-0103)

The core is the spine's recorder: modules announce world changes with
[`emit_event`](../reference/events.md#emit_event), and the core keeps the copy of record in
Postgres. "What happened" is a question you ask the `module_events` table, not the bus.

**Delivery is at-least-once** (#832). On startup the core provisions the JetStream stream
`EPICURUS_EVENTS` over `*.events.>` — idempotent, so an existing stream is adopted, and one
whose config cannot be updated is kept with a warning rather than failing the boot — and
binds the durable pull consumer **`core-event-intake`**. The cursor lives on the server, so
a core that was down while a module emitted picks those events up on the way back up. See
[events → delivery posture](../reference/events.md#delivery-posture) for the exact
guarantee, including what the *publish* side does not promise.

`EventIntake` **consumes `*.events.>`** — one consumer, **every tenant**
(`EventBus.pull_subscribe_any_tenant`). This deliberately departs from the inbound-messaging
consumer's per-tenant subscribe: a tenant added at runtime would otherwise be silently
unheard until restart, and an intake that drops a tenant's events looks exactly like a
tenant that emitted none. It is core-only by construction — on the bus the core
authenticates with unrestricted pub/sub while a module is confined to its own tenant-scoped
subjects (ADR-0066).

Per message it: parses the [`EventEnvelope`](../reference/events.md#eventenvelope) (whose
validators reject an oversized or credential-carrying payload *on the way in*, so the
contract is enforced rather than trusted); checks that the **subject's tenant and the
envelope's `tenant_id` agree** — two independent claims, and a mismatch is dropped rather
than filed under a guess; records it in `module_events`; **acks**; then fans it out to the
live feed and to any registered `on_event` listener.

The message's disposition follows the durable write, and that ordering *is* the guarantee:

| Outcome | Disposition | Why |
| --- | --- | --- |
| Row committed, or a duplicate the unique constraint rejected | **ack** | It is on file either way. |
| The store failed — database down | **nak** (5s delay) | Nothing is durable yet. A database outage must cost latency, not history; redelivery is unlimited, so a long outage is survivable. |
| Malformed JSON, a failed envelope validator, or a tenant mismatch | **term** | It will never become valid, and unlimited redelivery with no terminal case is an infinite loop. One module's bad emit must not wedge intake for every other module. |

Fan-out happens **after** the ack on purpose: a slow listener holding the ack would blow
through `ack_wait` (30s) and trigger a redelivery that dedup then absorbs as a no-op —
skipping the listeners anyway. Nothing is acked before the row exists, so a core killed
mid-message gets that message back.

`on_event(listener)` is the seam consumers attach to (the automations engine, #666). It
fires only for **newly-stored** events: a duplicate is not a change, so a consumer never
sees the same one twice — which is also what makes a redelivery invisible to a listener
rather than a double-trigger.

`EventRetention` prunes the log on an hourly loop to `EVENTS_RETENTION_DAYS` (default 30;
`0` disables).

The feed is at `GET /platform/v1/events[/stream]` — the Observability screen's **Events**
tab (see [observability](../reference/observability.md#raw-events-feed-adr-0103)). The event catalog
lives in [events](../reference/events.md#the-event-catalog).

### Automations engine (ADR-0105)

The spine's first consumer, and what the whole event-driven block exists for: the core
decides whether a world change deserves an action, does it at an autonomy level the operator
chose, and writes down what it did. Full reference: [automations](../reference/automations.md).

`AutomationMatcher` attaches to the intake's `on_event` seam — so the spine stays unaware
anything consumes it — and drops matched triggers on a durable Postgres queue (the ADR-0051
pattern). `AutomationScheduler` is one poll loop draining that queue (closing digest
windows) and firing schedule triggers; it **replaces** the scheduled-turns loop.
`AutomationRunner` runs one automation: an agent turn, then a deterministic sink fan-out,
then a ledger entry — always a ledger entry.

**The sinks (#672, #723).** The **chat** sink is *turn-time*: the run persists into a session — so a
rolling chat is reply-able and the next run sees the reply — **only** when chat is configured,
never otherwise (the owner rule: an unchecked chat sink makes zero sessions). Its session→automation
mapping (`automation_sessions`) is what badges and groups automation chats in the list; the post-run
dispatcher therefore **skips** chat and the runner records it fired. The **notes**/**kb** sinks route
a run's output into a module document through the *existing* `ModuleRegistry.save_page_doc` (the #541
no-second-write-path rule), at a per-automation `DocumentTarget` (`{path_pattern, mode}`), recording
an `EntityRef` on the run's `artifacts` so the runs feed links what was written. The **push** sink
(`automations/push_sink.py`, #723) closes the last gap the seam left open: it calls
[`PushService.notify`](#push-notifications-adr-0102) under the `"automation"` category (with
`automation_id`, so a per-automation override can silence just one's *push*), title the
automation's own name, body the run's raw output — quiet hours, the rate cap, and the push
toggles all apply exactly as for any other caller. `notify()` always writes a
notification-center row (#797 — the center is a superset of push), and the sink records its id
as an `EntityRef` (`module="core"`, `kind="notification"`) on the run's `artifacts`, the same
field notes/kb already populate — so a `sinks=["push"]` template (all ten #717 shipped with)
always leaves a durable, linkable record even when push delivery itself is off or fails.

**Agent-gated delivery (#706).** The sink fan-out above is deterministic by default — but a
per-automation toggle (`agent_gated_delivery`, off by default) lets the run's own turn decide.
When on, `Agent._loop` splices a run-scoped `finish_quiet(reason)` spec into the turn **only when
both `automation_id` and `quiet_capable` are set** — the same "bound at the tool surface, not by
prompt politeness" discipline as the autonomy dial below, and deliberately *not* a `McpHost`
built-in: `register_builtin`'s tools are filtered only by the read/propose/write `allow` class,
and an ordinary chat turn passes `allow=None` (no filtering at all), so a globally-registered tool
cannot be automation-only. The call is intercepted by name before it would reach `_invoke` — never
routed, so it can't be mistaken for a module tool and never counts toward the loop guard's
consecutive-error streak. Calling it sets `AgentTurn.quiet`/`quiet_reason`; the runner reads that
back to skip `SinkDispatcher.dispatch` (`push`/`notes`/`kb`) and records the ledger entry with
outcome `quiet` instead of `ok` — always a ledger entry, exactly as for any other outcome. **`chat`
is the one sink `quiet` does not touch**: its session is decided *before* the turn runs (the
paragraph above), so it persists and is recorded as fired regardless of the quiet decision — rolling
continuity needs the next run to see this one's reply. Not calling the tool delivers exactly as
before (fail-loud beats fail-silent). Full reference:
[automations → Agent-gated delivery](../reference/automations.md#agent-gated-delivery-706).

**The autonomy dial is enforced here, not requested.** An automation's level derives a set
of allowed tool *classes* (`read` / `propose` / `write`, declared on each
[`ToolSpec`](../reference/modules.md#side_effect--what-a-tool-does-to-the-world-adr-0105)),
and `Agent.run(allow=…)` passes it to `McpHost.discover`, which filters **both** the specs
the model sees **and** the `route` the agent dispatches on. A withheld tool is unroutable:
a model that names it anyway gets `error: unknown tool`, and nothing runs. This is the same
posture as the draft-first guarantee — *"the guarantee is the contract, not a prompt"*.

Because MCP's `list_tools` carries no manifest annotation, the classification is resolved
registry-side (`ModuleRegistry.tool_side_effects`, over the TTL-cached snapshot) — the same
reason `document_tool` exists — and only when a turn actually passes `allow`, so ordinary
chat pays nothing. The core built-ins are classified at registration: `now` and
`memory_search` read; `propose_automation` propose; `remember`, `ask_user`, and
`set_chat_model` write. (`finish_quiet` and `ask_approval` carry no such classification at
all — neither is a `register_builtin` tool, see *Built-in agent tools* above.)

**Safety:** a **persisted** per-tenant kill switch (unlike `PowerController`, which resets
on restart — a stop a restart undoes is not a stop), rate caps, digest windows, a
rate-limited `core.automation_failed`, and a **depth-1 loop guard**: an event a run produces
carries a `causation_id`, and the matcher refuses any event that carries one, so automations
cannot spiral.

**Metering is dual:** `automation_runs` and `UsageEvent` both name the tenant *and* the
automation — without the second, an automation quietly burning tokens is indistinguishable
from the operator's own chatting.

**Scheduled turns folded in** at startup (idempotent, non-destructive): #614's rows became
schedule-triggered automations with a rolling chat sink, keeping their cadence, session,
enabled flag, and last-run stamp. `ScheduledTurnScheduler` still exists for the un-migrated
path but new work creates an automation.

### Push notifications (ADR-0102)

`PushService.notify(tenant, category=..., title=..., body=...)` (`push/service.py`) is the
core-internal send path a category-based caller uses in-process (#670) — today, the settings
UI's test button and the [automations engine's push sink](#automations-engine-adr-0105)
(#723). Every call first records a notification-center row (`notifications.py`)
**unconditionally** (#797, amending ADR-0102 §4/ADR-0104 §1): **the center is a superset of
push** — the durable log of every notification that fired — and `push` means "also deliver
to devices", so a push missed while the device was off (or that failed outright) is never
simply gone. `ChannelPrefs.center` is still stored and carried on the wire for contract
compatibility but no longer consulted by delivery. `NotifyResult.notification_id` carries the
row's id back to the caller (always set), so a caller like the push sink can build an
`EntityRef` without a second lookup. Push delivery then resolves, in order: the effective
push toggle (off skips delivery entirely), quiet hours in the tenant's timezone (ADR-0039 —
queues for a digest instead of sending), and an in-memory per-tenant rate cap
(`PUSH_RATE_CAP_PER_HOUR`, single-instance v1 — the same disposable-cache trade the live-run
registry makes, ADR-0055). Delivery fans out to every device via VAPID-signed webpush
(RFC 8291/8292) — the outgoing payload's `body` is capped at 500 characters (an automation's
full report should not be able to blow a push service's own size ceiling), independent of the
untruncated text already written to the notification-center row above — pruning any
subscription the push service reports Gone (404/410) as expected churn (uninstalled PWA,
cleared site data), not an error.

**Every declined or deferred push logs a distinct line** (#797), so an operator can tell
"working as configured" from "broken" straight from the logs: `push skipped: push disabled
for this notification` · `push queued for quiet-hours digest` · `push rate cap reached;
delivery skipped` · `push skipped: no registered devices` (warning — a push was wanted with
nowhere to go) · `push send failed` · `pruned dead push subscription`, plus the event-alert
listener's `event alert declined: no subscription for this event` / `event alert rate cap
reached`. The most recent delivery *attempt* per tenant (anything except a disabled skip) is
kept in memory and served by `GET /platform/v1/push/status` — the settings card's
delivery-state readout (device count + what happened last, `null` after a restart).

`/platform/v1/push/*` (`push/routes.py`) is the subscription/preference surface the PWA's
service worker and Settings page drive: `GET /vapid-public-key`, `GET`/`POST`/`DELETE
/subscriptions[/{sub_id}]`, `GET`/`PUT /prefs`, `GET`/`PUT /event-subscriptions` (below),
`POST /test` (the settings UI's "send test notification" button, category `"system"` — its
response carries `failed_count` alongside the sent/pruned counts since #797), and
`GET /status` (above). `/platform/v1/notifications/*` (`notifications_routes.py`) is the
notification-center half: `GET ""` (list), `GET /unread-count`, `POST /{id}/read`,
`POST /read-all`. See [the reference page](../reference/notifications.md) for the full
contract and the web-side subscribe flow.

**Event alerts (#732, ADR-0114).** `push/event_subscriptions.py` + `push/event_alerts.py`:
a tenant-scoped `(module, event_type) -> ChannelPrefs` store, off by default, and a listener
wired to the same `EventIntake.on_event` seam the automations matcher uses — a dumb fan-out,
not an automation (no agent turn, no ledger). `PushService.notify_effective` is the send path
for a caller (this listener) whose channel prefs come from somewhere other than `PushPrefs`
categories. An automation triggered by the same event still fires independently — two
notifications, by design. See [the reference page](../reference/notifications.md#event-alerts-732-adr-0114)
for the full contract.

### Core-emitted spine events (#665)

The core also **emits** (`core_events.py`, `CoreEventEmitter`) — over its own bus, exactly
like a module would, so its events flow through the same intake → log → feed path as
everyone else's:

- **`files.file_added` / `files.file_deleted` / `files.file_moved`** — at the file-API
  seam (`files_routes.py`): the core owns the file space (#434), and every mutation —
  operator upload/delete, module-bridge write/delete/move (`PlatformClient.files_*`), the
  object-store fallbacks — passes through those handlers. There is deliberately **no
  `file_updated`** (an overwrite emits nothing — content owners announce their own
  `*_updated`), and out-of-band disk changes seen only by the file watcher are not emitted.
  A mutation inside an external mount (#731) emits the same way, with a `mount:<name>/`-
  prefixed path — unconditionally, independent of that mount's indexing opt-in (the two are
  orthogonal: an event says something changed, indexing says it's searchable).
- **`core.suggestion_approved` / `core.suggestion_rejected`** — at
  `ModuleRegistry.review_action`, the one funnel every review surface passes through:
  module review pages proxied over HTTP *and* the in-process core pseudo-module
  (ADR-0093 §2). One decision, one event, whichever surface the operator used; the
  payload lifts `operation`/`path` from the surface's `ApplyResult`.

Every emission is best-effort — a spine hiccup is logged and never fails the mutation or
decision that already landed. Payload shapes and dedup keys are in the
[event catalog](../reference/events.md#the-event-catalog).

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
| `ASK_APPROVAL_TTL_HOURS` | `24` | How long a turn paused by `ask_approval` waits for Approve/Reject before its pending approval is reaped — expiry decays to the existing async review-queue behavior, nothing lost (#745, ADR-0117). |
| `LIVE_RUN_GRACE_SECONDS` | `300` | How long a *finished* in-flight run stays re-attachable in memory before it is reaped (ADR-0055). Pure cache — the answer is already durable, so this only bounds how long a late re-attach can tail the buffer. |
| `EVENTS_RETENTION_DAYS` | `30` | How long a module event stays in the durable log (ADR-0103). `0` disables pruning — keep everything, and mind the disk. |
| `EVENTS_PRUNE_INTERVAL_S` | `3600` | How often the event-log pruner sweeps. |
| `AUTOMATIONS_POLL_INTERVAL_S` | `60` | How often the automations loop drains the trigger queue and checks schedules (ADR-0105). |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Conversation persistence. |
| `QDRANT_URL` | `http://qdrant:6333` | Semantic-recall vectors. |
| `MEMORY_EMBED_MODEL` | `nomic-embed-text` | Local embedding model for recall. |
| `MEMORY_EXTRACTION_MODE` | `nightly` | When fact extraction runs: `nightly` (deferred to a queue drained off-hours, ADR-0051) or `immediate` (a background task after each turn, ADR-0045). |
| `MEMORY_EXTRACTION_HOUR` | `3` | Local hour (0-23) of the nightly drain, in the operator's timezone. |
| `MEMORY_EXTRACTION_MODEL` | — | Optional small dedicated model for the extraction call (e.g. `llama3.2:3b`); blank = the default chat model. |
| `MEMORY_EXTRACTION_BATCH_LIMIT` | `200` | Max exchanges distilled per nightly drain. |
| `MEMORY_RECALL_TIMEOUT_S` | `4.0` | Time-box (seconds) for the inline recall embed before a turn proceeds without it (ADR-0051). 4s (was 2s) fits a single-GPU embed-model swap. |
| `MEMORY_PROFILE_MODEL` | `""` | Optional dedicated model for the nightly **standing-profile** synthesis (ADR-0094); blank = the operator's default chat model. A small model keeps the pass cheap. |
| `PLAYBOOK_REFLECTION_MODEL` | `""` | Optional dedicated model for the nightly **playbook-reflection** pass (ADR-0093); blank = the operator's default chat model. A small model keeps the pass cheap. No reflection *hour* knob exists — the pass rides the maintenance schedule. |
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
- **Postgres `automations`** — the automations engine's definitions (ADR-0105), tenant-scoped:
  `id` (opaque uuid hex) + internal `pk`, `name`, `enabled`, `source` (`user` /
  `template:<module>` / `agent`), JSON `event_trigger` **or** `schedule_trigger` (exactly one),
  `prompt`, `model`, `autonomy`, JSON `sinks`, JSON `sink_config` (#672 — the notes/kb
  document targets), `agent_gated_delivery` (#706 — the "agent decides delivery" toggle),
  `chat_mode`, `chat_session_id`, `rate_cap_per_hour`, `digest_window_minutes`, timestamps,
  `last_run_at` / `last_status`. The last two columns post-date the table and are reconciled
  additively (ADR-0067), as any further one must be. The
  triggers are JSON rather than flattened columns: a trigger is a closed vocabulary the core
  owns and always reads whole, so flattening would buy nothing and cost a migration per new
  matcher op.
- **Postgres `automation_runs`** — the run ledger. Written for **every** run at **every**
  autonomy level; for `silent_act` it is the only record that anything happened. Carries
  **both** attributions (`tenant` + `automation_id` — the SaaS metering point), `trigger_refs`
  (the `module_events` ids that caused it), `filter_verdict`, `model`, token counts, duration,
  outcome, error, the `output`, `sinks_fired`, JSON `artifacts` (#672 — the `EntityRef`s the
  run wrote, which the runs feed links), and `quiet_reason` (#706 — why a `quiet` outcome
  skipped delivery). The last two are additively reconciled (ADR-0067).
- **Postgres `automation_queue`** — matched triggers awaiting a run (the ADR-0051 durable-queue
  pattern). The matcher runs on intake, the run may be much later (an open digest window), and
  a restart in between must lose nothing.
- **Postgres `automation_kill_switch`** — one row per tenant. Postgres, not memory: a safety
  stop that forgets itself on restart is not a safety stop.
- **Postgres `module_events`** — the module event spine's durable log (ADR-0103), the core's
  copy of record for every world change a module announced: `id`, `tenant`, `module`, `type`,
  `occurred_at` (the emitter's clock — when the change happened), `received_at` (ours — what
  retention prunes on, being the only one of the two guaranteed monotonic with respect to this
  table), `dedup_key`, JSON `entity_ref` (ADR-0019) / `payload`, `schema_version`. Tenant-scoped.
  `UniqueConstraint(tenant, module, dedup_key)` is what makes the log idempotent: a re-delivered
  change collapses to one row, decided by the database rather than a read-then-write check that
  races. **First write wins** — a duplicate never updates the stored row, because an event
  describes a change that already happened and a later delivery carries no newer truth. Bounded
  by `EVENTS_RETENTION_DAYS`.
- **Postgres `agent_attachments`** — the core-side handle for an uploaded chat attachment
  (ADR-0019): `att_id` (primary key), `tenant`, `kind` (the upload's MIME content-type), `title`,
  `content` (raw bytes), `created_at`. Written by `POST /agent/attachments`; read back once per
  turn by the attachment expander (`AttachmentStore.get`), scoped to the requesting tenant.
  `kind.startswith("image/")` is what routes a `file` attachment to the vision path instead of
  text expansion (#633) — see **Agent** above. There is deliberately no session column: the
  chat↔attachment link lives in `agent_messages.attachments` (JSON), which is why the delete
  cascade (#771) collects a session's `att_id`s from its messages *before* deleting them, then
  drops the byte rows here (the best-effort copy pushed to the storage sink's Files page is a
  separate durable artifact and is kept).
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
  `tenant`, `model`, `added_at` (epoch-ms, `BigInteger`, drives most-recent-first ordering), plus
  the capability override (#711) in `vision_override` (`"on"`/`"off"`/NULL = auto) and
  `context_length_override` (NULL = take LiteLLM's map) — both nullable and reconciled additively
  (ADR-0067), so NULL on both is the pre-override behaviour and forgetting a model forgets its
  override with it. Only hosted ids land here — a known `<provider>/` prefix; the route rejects
  locals so an `hf.co/…` model can't masquerade as hosted. A durable, cross-device home for the
  strings entered in the chat picker (the browser's `recentModels` is only a warm cache).
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
  movability stays authoritative (#560; see [file space](../reference/files.md)). An indexed
  **external mount** (#731) shares this same table, namespaced under `mount:<name>/` — opt-in
  per mount (`FILES_EXTERNAL_MOUNTS_INDEXED`), prefix-scoped on purge so re-scanning one mount
  never touches the tenant tree's rows or another mount's.
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
  `DEFAULT_AGENT_INSTRUCTIONS` — which establishes voice; tool use, including the #742
  verify-before-mutate rule (an entity known only from earlier turns, not a tool result just
  now, gets re-checked — list/stat/read/search — before a mutating call relies on it) and its
  recover-on-not-found counterpart (a tool reporting something missing means re-ground and
  report reality, never blind-retry the same call); and the source-grounding
  ladder (module data first, then web search, then — since #739 — *reading* a link the message
  carries instead of guessing at what is behind it, keeping the source URL and retrieval date
  on anything filed into the knowledge base, and saying plainly what the link did not yield;
  never an unsourced guess, #703); resolved per turn
  and injected first in `Agent._assemble`. **Porting note (#742):** these are prompt *text*,
  not code — a tenant that has already replaced the default via `PUT /agent/instructions` does
  not pick up new rules automatically. An operator running a heavily customized prompt should
  port the verify-before-mutate/recover-on-not-found paragraph (or the gist of it) into their
  own instructions if they want the same behavior; there is no mechanism that layers the shipped
  default's rules onto a custom one.
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
- **Postgres `agent_reflection_state`** — the nightly reflection pass's per-tenant watermark
  (ADR-0093 §1): `tenant`, `last_run_at`. Durable rather than in-memory (constraint #2): an
  in-process marker would reset on every restart and re-scan the whole history, re-proposing
  lessons the operator has already seen. Snapshotted before a scan and advanced only on a
  completed pass. Read back as **aware UTC** regardless of backend — Postgres returns an aware
  datetime, SQLite a naive one, and comparing the two raises.
- **Postgres `agent_suspended_runs`** — a turn paused by `ask_user` (ADR-0053): `id` (run_id),
  `tenant`, `session_id`, `model`, `pending_call_id`, `question`, `conversation` (JSON),
  `created_at`. Written on suspend, **consumed** on resume, reaped after `ASK_USER_TTL_HOURS`.
- **Postgres `agent_pending_drafts`** — a turn paused on a draft-first send (ADR-0085, #563):
  `id` (run_id), `tenant`, `session_id`, `model`, `pending_call_id`, `tool`, `module`, `summary`,
  `draft` (JSON — the composed message), `conversation` (JSON), `created_at`. A **sibling** of
  `agent_suspended_runs` (a separate table, so `create_all` builds it with no migration and the two
  consume-on-resume paths can't cross). Written on suspend, **consumed** on Confirm/Decline, reaped
  after `DRAFT_REVIEW_TTL_HOURS`.
- **Postgres `agent_pending_approvals`** — a turn paused by `ask_approval` (#745, ADR-0117): `id`
  (run_id), `tenant`, `session_id`, `model`, `pending_call_id`, `summary`, `refs` (JSON — the
  entity reference(s), possibly empty), `conversation` (JSON), `created_at`. A **third sibling**
  of `agent_suspended_runs`/`agent_pending_drafts`, same reasoning: a separate table so
  `create_all` needs no migration and none of the three consume-on-resume paths can cross. No
  `tool`/`module` column like the draft table — the pending call is always the one fixed
  `ask_approval` tool, never a per-module compose tool. Written on suspend, **consumed** on
  Approve/Reject, reaped after `ASK_APPROVAL_TTL_HOURS`.
- **Postgres `session_models`** — a session's persisted model override (#707): `session_id`
  (primary key), `tenant`, `model`, `updated_at`. A row exists only once a session's model has
  been explicitly set — by the `set_chat_model` tool or an explicit picker change (the same
  `SessionModelStore.set`, so the two paths share one owner of truth) — never for an ordinary
  session; `GET /sessions` left-joins it in for the picker's read-back. The sidecar exists
  because there is no other "session row": a session is derived from `agent_messages` via
  `GROUP BY` (`ConversationStore.sessions`), the same reason `automation_sessions` (above)
  needed its own table for the chat-list badge.
- **Postgres `ephemeral_sessions`** — the invisible-chat flag rows (#772): `session_id`
  (primary key — session ids are client-minted uuids, so a cross-tenant collision is
  unrepresentable, the `session_models` precedent), `tenant`, `created_at`. A flagged session
  writes normally but is excluded from the sessions list, the extraction enqueue, reflection's
  transcript scan, and `memory_search`'s conversation half; every exit path deletes it via the
  #771 cascade (which drops this row **last**, so a failed erase stays sweepable), and the
  orphan sweep erases any flagged session a crashed client left behind. A new table created by
  `create_all` — no migration.
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
  extraction (ADR-0051): `id`, `tenant`, `user_text`, `assistant_text`, `created_at`, and a
  nullable `session_id` (#771) stamping each exchange with the conversation it came from — the
  handle that lets the chat delete cascade purge a deleted session's still-queued exchanges
  before the drain distils them. Reconciled additively at init (ADR-0067): rows enqueued before
  the column existed stay `NULL` and drain exactly as before (they simply can't be targeted by
  a delete). In the default **nightly** mode the agent enqueues each exchange here instead of
  distilling it inline; the `ExtractionRunner` drains it once a day (at
  `MEMORY_EXTRACTION_HOUR` in the operator's timezone), serially, so extraction never competes
  with a live turn for the GPU. Drained rows are deleted; because the queue is durable, a
  restart never loses a pending exchange.
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
