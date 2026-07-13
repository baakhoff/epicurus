# Mail module

## What it is

The **mail module** lets the agent search, read, and — **draft-first** — compose sends and
replies on the user's behalf. The module is **provider-agnostic**: tools are named and typed in
terms of the mail domain (`mail_search`, `mail_read`, `mail_send`, `mail_reply`), and the
underlying provider is pluggable via the `MailProvider` interface (ADR-0016).

The agent **never sends mail on its own** (ADR-0085, #563). `mail_send` / `mail_reply` *compose*
a message and return it as a pending draft; the core pauses the turn, the shell shows the draft
in a split-pane, and only the operator's **Confirm** triggers the actual send (**Decline** sends
nothing). The MCP surface exposes **no** tool that transmits — the sole send path is the module's
internal `POST /send` endpoint, which the core calls after Confirm. The guarantee is structural,
not a prompt.

Gmail is the v0.1 provider.  Tokens are fetched at runtime from the core's OAuth
vault — the module never holds a client secret or refresh token (see
[OAuth reference](../reference/oauth.md)).  Future providers (IMAP/SMTP,
Microsoft) will implement the same interface without changing the tools.

**v0.2.0** (Phase 3.8): `mail_search` results now surface as entity-reference chips
in the chat UI (ADR-0019).  Hover shows a compact preview; clicking opens the full
message in the right-panel `email-reader` view (ADR-0018), read-only.  The module
now declares `resolver: true` and serves `GET /resolve/message/{ref_id}` (hover-card)
and `GET /messages/{ref_id}` (full email for the panel).

**v0.4.0** (Phase 3.8): the hover-card resolver now reports the message's **unread**
status.  When a referenced message is unread, the `HoverCard` leads its detail rows
with `Status: Unread`; read messages omit the row rather than render a redundant
"Read".  The provider-agnostic `MailMessage` gains an `unread` flag the Gmail
provider derives from the `UNREAD` label.  The chip's click still opens the
read-only `email-reader` panel directly, so the resolver carries no `href` (there is
no outbound URL — the reader is in-app panel navigation).  Mail skips a 0.3.0
"attach" step because it is read-only, jumping 0.2.0 → 0.4.0.

**v0.7.0** (#277): mail is no longer read-only — messages can be **marked read / unread**.
Two new MCP tools (`mail_mark_read`, `mail_mark_unread`) let the agent flip read state on
request, and the right-panel `email-reader` now renders a **Mark as read / Mark as unread**
toggle (a tool-backed action, ADR-0024): pressing it invokes the matching tool through the
core proxy and re-fetches the message so the toggle flips. The provider seam gains
`set_unread(message_id, unread)`; the Gmail provider implements it via `messages.modify`
on the `UNREAD` label, which requires the **`gmail.modify`** scope (replacing `gmail.readonly`,
which it supersets). **Operators who connected Google before v0.7.0 must reconnect once**
(Settings → Connect) to grant `gmail.modify`; until then the mark tools return a reconnect hint.

**v0.8.0** (#461): mail can now **reply** in an existing thread, not just start new
conversations. The new `mail_reply(message_id, body)` tool fetches the original message's
`Message-ID`/`References`/`Subject`/`From` (a lightweight metadata-only call — no body fetch),
then sends with RFC-2822 `In-Reply-To`/`References` headers (chaining the full reference list,
not just the immediate parent) and the Gmail `threadId` in the send payload, so the reply lands
in the same conversation for both Gmail and any RFC-2822-compliant client. The recipient (the
original sender) and subject (`Re: <original>`, not doubled if already a reply) are derived —
the caller supplies only the new body. Declared a **danger action** (ADR-0007) exactly like
`mail_send`. The provider seam gains `MailProvider.reply(message_id, body)`, so a future
non-Gmail provider implements the same threading contract.

**v0.8.1** (#513) hardens reply/send: the recipient now honors the original message's
`Reply-To` header over its `From` when both are present (mailing lists, newsletters, and
support desks commonly set `Reply-To` to route replies away from the sending address); a 403
from Gmail (a `gmail.send`-less token) returns the same reconnect-hint treatment
`mail_mark_read`/`mail_mark_unread` already have for `gmail.modify`, instead of a bare
exception; and a self-reply (the operator replying to their own message) is deliberately left
unguarded — documented as a considered decision rather than an oversight, since it is
indistinguishable from mailing yourself a note and the danger-action confirm already shows
the recipient before anything sends.

**v0.8.2** (#538, #539): a Gmail 403 is no longer automatically treated as a missing scope —
the error body's reason is inspected first, so per-user/per-day rate limiting (`usageLimits`)
now returns a distinct "wait and try again" hint instead of a misleading "reconnect Google"
one. `mail_reply` specifically makes two Gmail calls (a metadata lookup needing `gmail.modify`,
then the send itself needing `gmail.send`); a 403 is now attributed to whichever of the two
actually failed, rather than always blaming the send scope. A whitespace-only `Reply-To`
header (present but blank) no longer wins over `From` — it is stripped before the precedence
check, since a non-empty string of only spaces is still truthy in Python. Separately,
`mail_search`'s listing text now goes through the shared `epicurus_core.capped_listing`
helper (#468/ADR-0084) instead of hand-rolling it, matching `calendar_list_events`'s adoption
— a no-op in practice today since `max_results` is already clamped to the same 50-item cap,
but it keeps the two modules from drifting apart on how a capped list reads.

**v0.9.0** (#563, ADR-0085): **draft-first send/reply — the agent can no longer send mail directly.**
`mail_send` / `mail_reply` stop calling the provider's send; they *compose* the full message (a
reply still derives recipient/subject/`In-Reply-To`/`References`/`threadId` from the original, now
a **read**) and return a `DraftReview` envelope (`epicurus_core.draft_review`). The core recognizes
it, **pauses the turn** (the same durable machinery as `ask_user`, ADR-0053), and the shell renders
the draft in the split-pane for **Confirm** / **Decline**. The only transmitting path is the new,
internal **`POST /send`** endpoint — not an MCP tool, so the model cannot reach it; the core calls
it on Confirm and appends the outcome to the turn. The provider seam swaps `send`/`reply` for
`compose_reply` (read + derive) and `transmit` (send-only, the `/send` backing). The two
Modules-page actions are reframed from one-tap danger sends to **compose-for-review** (no longer
`intent="danger"`). `mail.sent` is now actually published — at `/send`, the one point a message
is really sent. Where the pending draft lives (core-held, not a Gmail draft), why edit-before-send
is deferred (#542), and the Decline-reason are all recorded in ADR-0085.

**v0.9.1** (#557) widens the rate-limit handling from v0.8.2. The 403 inspection now recognizes
**both** error shapes: the legacy Gmail `error.errors[].reason` array *and* the modern AIP-193
shape (`error.status == "RESOURCE_EXHAUSTED"` / `error.details[].reason == "RATE_LIMIT_EXCEEDED"`),
so a throttled 403 keeps its "wait and try again" hint even if Gmail migrates shapes. An HTTP
**429** (Too Many Requests) — which none of the hint paths caught before — now returns the same
wait-and-retry hint on **every** Gmail-touching path (`mail_search`, `mail_read`, `mail_reply`,
`mail_mark_read`/`unread`, and the `/send` transmit), honoring `Retry-After` when present, instead
of a raw traceback (`mail_search`/`mail_read` previously had no HTTP-error handling at all). The
reason-membership test is also guarded so a non-string `reason` (a nested object in an otherwise
well-formed body) falls back to the scope hint rather than raising.

**v0.10.0** (#550, ADR-0087): **a full mail client in the shell — the `mailbox` page.** Mail is now
a first-class left-nav page like Files / Calendar / Tasks / Notes: a **labels rail → paginated
thread list → conversation** with compose and reply, rendered entirely by the core shell (the
module still ships **zero markup**, ADR-0018). It declares a new `mailbox` archetype and serves its
data over the page proxy:

- **Browse.** `GET /pages/mailbox?label=&q=&cursor=` returns the rail (folders with unread counts
  for the active label + Inbox) and one **cursor-paginated** page of thread summaries. Browsing is
  folder-scoped; a search (`q`, Gmail syntax) spans the whole mailbox. Page size is capped so one
  fetch can't scan an unbounded mailbox (#539).
- **Read.** `GET /pages/mailbox?thread_id=` returns the full conversation — every message through
  the **same renderer** as the panel `email-reader` (not forked) — plus each message's attachments
  and a server-derived reply prefill. **Plain-text-first:** an HTML-only message is decoded to
  readable **text** server-side (`_html_to_text`, adversarial-tested) — no HTML is ever rendered in
  the shell, so there is no HTML-mail XSS surface. Rich sanitized-HTML rendering is a deferred
  follow-up.
- **Triage.** Two new message-level tools, `mail_archive` (drop the `INBOX` label) and `mail_trash`
  (move to Trash — recoverable, not a permanent delete), both inside the already-granted
  `gmail.modify` scope (no reconnect). They join `mail_mark_read`/`mail_mark_unread` as per-message
  `BoardAction`s in the thread pane.
- **Compose / reply.** A **human-initiated** page send: `POST /pages/mailbox/send` composes (or, for
  a reply, re-derives threading via `compose_reply`) and transmits. It **shares the transmit path
  but never the agent draft pane** (ADR-0085): the operator is the send button, gated by a Send
  confirm, and the endpoint is operator-only (never an MCP tool → the agent still cannot send).
- **Attachments** stream through the core proxy (`GET /pages/mailbox/attachment`), provider → module
  → browser — nothing is stored.

The `MailProvider` seam gains `list_labels` / `list_threads` / `get_thread` / `archive` / `trash` /
`get_attachment` (typed in mail-domain terms; a future IMAP/Microsoft provider capability-gates
rather than forcing symmetry). Deferred: thread-level bulk triage, the unread-count nav badge, and
rich HTML rendering.

---

## Contract

### MCP tools

All tools operate on the active `MailProvider` for the tenant.

| Tool | Inputs | Output | Notes |
| --- | --- | --- | --- |
| `mail_search` | `query: str`, `max_results: int = 10` | `ToolEnvelope` (text + entity refs) | Returns entity-ref chips; no body. Gmail query syntax. Max 50. |
| `mail_read` | `message_id: str` | `str` (formatted text) | Subject, sender, date, and decoded plain-text body for the agent to reason on. |
| `mail_send` | `to: str`, `subject: str`, `body: str` | `DraftReview` | **Compose-only (draft-first, ADR-0085)** — does **not** send. Composes the message and returns a `DraftReview` envelope; the core pauses the turn for the operator's Confirm/Decline. Rejects a blank recipient. |
| `mail_reply` | `message_id: str`, `body: str` | `DraftReview` | **Compose-only (draft-first)** — does **not** send. Composes a reply in *message_id*'s thread (RFC-2822 `In-Reply-To`/`References` + provider thread association; recipient/subject derived from the original) and returns a `DraftReview`. The compose-time metadata read needs `gmail.modify`. |
| `mail_mark_read` | `message_id: str` | `str` | Clears the unread flag (`messages.modify`). Returns `"marked-read:<id>"`. Distinct from `mail_read` (which fetches the body). Idempotent. |
| `mail_mark_unread` | `message_id: str` | `str` | Restores the unread flag. Returns `"marked-unread:<id>"`. Idempotent. |
| `mail_archive` | `message_id: str` | `str` | Removes the `INBOX` label — archives out of the Inbox without deleting (`messages.modify`). Returns `"archived:<id>"`. Idempotent (ADR-0087). |
| `mail_trash` | `message_id: str` | `str` | Moves the message to Trash — recoverable, **not** a permanent delete (`messages.trash`). Returns `"trashed:<id>"`. Idempotent (ADR-0087). |

`mail_mark_read` / `mail_mark_unread` require the `gmail.modify` scope; on a 403 (a Google
account connected before v0.7.0, lacking the scope) they return a reconnect hint instead of failing.
`mail_reply`'s compose-time metadata read also needs `gmail.modify`, and returns the same reconnect
hint on a 403 (it never sends, so it can never be a send-scope error). The **send** scope
(`gmail.send`) is exercised only by `POST /send` (below), which returns the equivalent hint on a
403. A 403 caused by Gmail rate limiting (`usageLimits`, not a scope problem) returns a distinct
"wait and try again" hint instead (#538) — the error body's reason decides which hint applies, so
throttling is never misreported as a missing scope. The rate-limit reason is read in **both** the
legacy (`error.errors[].reason`) and modern AIP-193 (`error.status`/`error.details[].reason`)
shapes, and an HTTP **429** on any of these paths — including `mail_search`/`mail_read`, which
otherwise had no HTTP-error handling — returns the same wait-and-retry hint (honoring `Retry-After`)
rather than a raw exception (#557).

`mail_reply` composes the reply body **clean** — it is never auto-quoted with the original
message's text — and addresses the original's `Reply-To` header when present, falling back to its
sender (#513). Replying to a message the operator sent themselves is allowed with no special guard:
it is indistinguishable from deliberately mailing yourself a note, and every reply is shown in the
split-pane for Confirm/Decline before anything sends (ADR-0085).

`mail_search` returns a `ToolEnvelope` (ADR-0019): the `text` field is a human-readable
summary; `entity_refs` carries one `EntityRef` per message (`module="mail"`,
`kind="message"`) so the UI renders interactive chips — no body content is
transferred to the model context.

`mail_send` / `mail_reply` return a **`DraftReview`** (ADR-0085) — `{kind: "mail", module: "mail",
summary, draft}`, where `draft` is the composed `ComposedMessage` (`to`/`subject`/`body`, plus a
reply's `cc`/`in_reply_to`/`references`/`thread_id`/`reply_to_original`). The core recognizes the
envelope, persists the draft on the suspended run, and emits an `awaiting_input` frame with
`awaiting_kind: "draft_review"` + the draft; on Confirm it POSTs that same draft to `/send`, so the
bytes sent are byte-identical to what the operator reviewed.

### HTTP endpoints (internal)

The module serves these HTTP endpoints on the internal Docker network; the core
proxies them to the web shell (the shell never calls the module directly).

| Method | Path | Shape | Purpose |
| --- | --- | --- | --- |
| `GET` | `/resolve/message/{ref_id}` | `HoverCard` | Hover-card resolver (ADR-0019). Returns subject, snippet, sender, recipients, date, and unread status (a `Status: Unread` row, only when unread). No `href` — the chip's click opens the reader. |
| `GET` | `/messages/{ref_id}` | `EmailMessage` | Full email for the panel's `email-reader` view. Returns subject, from, date, body, `module`/`message_id`, the `unread` state, and a one-element `actions` toggle (mark read/unread, ADR-0024). |
| `POST` | `/send` | body: `ComposedMessage` → `{"id": str}` | **The module's only send path (ADR-0085, #563).** Transmits an operator-confirmed draft verbatim and publishes `mail.sent`. Not an MCP tool, so the agent cannot reach it — the core calls it after the operator Confirms a draft in the split-pane. A 403 maps to the same reconnect / rate-limit hint the tools use (#513/#538), as an HTTP 403. |
| `GET` | `/status` | `{"gmail_connected": bool}` | Whether a Google token is available — a fast token-presence check (`is_available`), **not** a live Gmail API call (#209), so the polled status panel can't stall the core's status proxy into a Bad Gateway. Proxied by the core. |
| `GET` | `/pages/mailbox` | `MailboxList` or `{thread: MailThread}` | **The `mailbox` archetype's data (ADR-0087).** With `?thread_id=` returns one full conversation; otherwise the rail + a cursor page of threads (`?label=`, `?q=`, `?cursor=`). The plain landing view (no `q`/`cursor`) serves from the **local cache** instantly (ADR-0096, #623); `?reconcile=1` first pulls the provider delta into the cache. Reached through the generic page proxy (query params forwarded, ADR-0023). A Gmail scope/rate-limit error relays its hint under Gmail's status. |
| `POST` | `/pages/mailbox/send` | body: `MailboxSend` → `{"id": str}` | **Human-initiated compose/reply from the page (ADR-0087).** With `reply_to_message_id` re-derives threading via `compose_reply`, else composes from `to`/`subject`/`body`/`cc`; then transmits and publishes `mail.sent`. Operator-only via the gated core proxy — never an MCP tool, so the agent still cannot send (ADR-0085). |
| `GET` | `/pages/mailbox/attachment` | `?message_id=&attachment_id=` → bytes | Streams one attachment's bytes (with content-type + download disposition) for the core proxy to relay; nothing is stored (ADR-0087). |

The core exposes these via:

```
GET  /platform/v1/modules/mail/resolve/message/{ref_id}        → HoverCard
GET  /platform/v1/modules/mail/messages/{ref_id}               → EmailMessage
GET  /platform/v1/modules/mail/status                          → status JSON
GET  /platform/v1/modules/mail/pages/mailbox[?thread_id|label|q|cursor|reconcile]  → MailboxList | {thread}
POST /platform/v1/modules/mail/pages/mailbox/send              → {"id": str}   (mailbox-gated)
GET  /platform/v1/modules/mail/pages/mailbox/attachment        → streamed bytes (mailbox-gated)
```

`POST /send` is **not** proxied to the shell — it is the core-internal transmit hop invoked by the
draft-review Confirm (`ModuleRegistry.send_draft`, ADR-0085). The mail page's own compose (#550,
ADR-0087) instead posts to `…/pages/mailbox/send` above: a **human-initiated** send that shares the
module's transmit but never the agent draft pane. The `send` and `attachment` proxies are gated on
the `mailbox` archetype (a non-mailbox page 404s), mirroring the editor doc gate.

#### `mailbox` archetype shapes (ADR-0087)

The list read (no `thread_id`): the rail carries `unread` only where cheaply known (the active
label + Inbox); pagination is by opaque `next_cursor` (never offset). `sort_ts` is the thread's
last-message epoch **milliseconds** — the local cache's ordering key (ADR-0096, #623), `0` when
the provider didn't supply one.

```json
{
  "title": "Mail",
  "labels": [{ "id": "INBOX", "title": "Inbox", "kind": "system", "unread": 2 }],
  "active_label": "INBOX",
  "query": "",
  "threads": [
    { "id": "t1", "subject": "Project kickoff", "sender": "alice@example.com",
      "snippet": "Let's get started", "date": "Mon, 1 Jan 2024 10:00:00 +0000",
      "unread": true, "message_count": 2, "sort_ts": 1704106800000 }
  ],
  "next_cursor": "PAGE2"
}
```

The thread read (`?thread_id=t1`): every message uses the `EmailMessage` shape (now extended with
`attachments`) so the page and panel share one renderer; `reply` is the server-derived prefill (the
send re-derives threading from `reply_to_message_id`, so the web never handles RFC-2822 headers).

```json
{
  "thread": {
    "id": "t1",
    "subject": "Project kickoff",
    "messages": [
      { "subject": "Project kickoff", "from": "alice@example.com", "date": "…",
        "body": "…", "module": "mail", "message_id": "m1", "unread": false,
        "attachments": [{ "id": "att1", "filename": "agenda.pdf",
                          "mime_type": "application/pdf", "size": 2048 }],
        "actions": [ { "tool": "mail_mark_unread", "…": "…" },
                     { "tool": "mail_archive", "…": "…" },
                     { "tool": "mail_trash", "intent": "danger", "…": "…" } ] }
    ],
    "reply": { "reply_to_message_id": "m1", "to": "alice@example.com",
               "subject": "Re: Project kickoff",
               "reply_to_original": "alice@example.com — Project kickoff" }
  }
}
```

`MailboxSend` (the `POST /pages/mailbox/send` body): `{ body, to?, subject?, cc?,
reply_to_message_id? }` — a reply sets `reply_to_message_id` (the module derives the rest); a fresh
compose sets `to`/`subject`.

#### `HoverCard` shape (from resolver)

The `Status: Unread` row leads the details and is present **only when the message is
unread**; a read message omits it. There is no `href` — clicking the chip opens the
read-only `email-reader` panel directly (in-app navigation, not an outbound URL).

```json
{
  "title": "Invoice from Acme",
  "description": "Please find attached…",
  "details": [
    { "label": "Status", "value": "Unread" },
    { "label": "From",  "value": "acme@example.com" },
    { "label": "To",    "value": "me@example.com" },
    { "label": "Date",  "value": "Mon, 1 Jan 2024 10:00:00 +0000" }
  ]
}
```

#### `EmailMessage` shape (from `/messages/{ref_id}`)

`actions` carries a single tool-backed toggle (ADR-0024) computed from `unread`: a **Mark as
read** action (`mail_mark_read`) when the message is unread, or **Mark as unread**
(`mail_mark_unread`) when it is read. `module`/`message_id` let the reader invoke the action
through the core proxy and re-fetch itself afterwards.

```json
{
  "subject": "Invoice from Acme",
  "from": "acme@example.com",
  "date": "Mon, 1 Jan 2024 10:00:00 +0000",
  "body": "Dear customer,\n\nPlease find the invoice attached.\n\nRegards",
  "module": "mail",
  "message_id": "msg1",
  "unread": true,
  "actions": [
    {
      "tool": "mail_mark_read",
      "label": "Mark as read",
      "intent": "default",
      "icon": "check",
      "args": { "message_id": "msg1" }
    }
  ]
}
```

### NATS events

| Subject (base) | Direction | Payload | Condition |
| --- | --- | --- | --- |
| `mail.sent` | emitted | `{"id": str, "to": str, "subject": str}` | After a confirmed send succeeds (published by `POST /send`, best-effort — a bus hiccup never fails a completed send) |

Subjects are tenant-scoped at runtime: `<tenant_id>.mail.sent`. Before v0.9.0 this event was
declared but never actually published; it is now emitted at `/send`, the one point a message is
really sent (ADR-0085).

---

## Local cache & incremental sync (ADR-0096, #623)

Before v0.11.0 the mailbox page fetched everything from Gmail on **every** open — the rail's
labels plus one metadata `threads.get` per thread, ~28 calls for a 25-row page — so opening Mail
was slow. The module now keeps a tenant-scoped **local cache** and reconciles it against the
mailbox incrementally.

- **On open — instant.** The plain landing view (default folder, no search, first page) serves
  the cached rows + rail with **no** provider call. The *first ever* open of a folder is a
  one-time cold sync that populates the cache; every open after renders in ~a second.
- **In the background — the delta only.** The web fires a second read with `?reconcile=1`. The
  orchestrator asks the provider what changed since the last sync (Gmail `historyId` via
  `users.history.list`) and rebuilds **only** the touched thread rows — a new message re-sorts to
  the top, a read/unread flip converges, an archived thread drops out, a deleted one is removed.
  When nothing changed it just advances the cursor (one cheap call). A cursor too old to replay
  (Gmail expires history after ~a week) or an IMAP `UIDVALIDITY` rotation triggers a full resync.
- **Provider-neutral.** The change cursor is a neutral `MailCursor {history_id, uid_validity,
  uid_next}` and the delta a thread-granular `ThreadChanges`, both behind the `MailProvider` seam
  — so a future IMAP provider fills `uid_validity`/`uid_next` and reuses the same cache unchanged.
- **Bounded to the landing view.** Search (`?q=`) and deeper pages (`?cursor=`) still read the
  provider live — the cache only accelerates the default landing page, which is the open path.
- **Read/unread converges both ways.** A mark-read is written through to the cache optimistically
  (the list reflects it before the provider round-trips); a mark made elsewhere flows back in
  through the next reconcile.

The orchestration lives in `epicurus_mail.cache.CachedMailbox`; the store in `epicurus_mail.db`.

---

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `PLATFORM_URL` | `http://localhost:8080` | Internal core base URL. On the Docker network: `http://core-app:8080`. |
| `DATABASE_URL` | `postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus` | Postgres DSN for the tenant-scoped local cache (ADR-0096, #623). The module owns its own `mail_*` tables in the shared Postgres — no shared database, just a shared server. |
| `DEFAULT_TENANT_ID` | `local` | Tenant this module acts on behalf of. |
| `NATS_URL` | `nats://localhost:4222` | NATS event backbone. |
| `LOG_LEVEL` | `info` | Logging verbosity. |
| `BIND_ADDRESS` | `127.0.0.1` | Host-side bind address for the published port. |
| `MAIL_PORT` | `8087` | Host-side port mapped to the container's 8080. |

No secrets are stored in environment variables — the access token is fetched at
call time from the core's OAuth vault.

---

## Data model

Message **content** is never persisted — the module is still a pass-through to the provider for
bodies and attachments, and the access token stays in the core's OpenBao vault
(`oauth/tokens/google` — see [OAuth reference](../reference/oauth.md)). A **pending draft**
awaiting Confirm/Decline is held **core-side** on the suspended run (`agent_pending_drafts`,
ADR-0085), not in the module.

What the module *does* own is the tenant-scoped **local cache** (ADR-0096, #623) — a
materialization of the landing view, not a mail store. Every table is scoped by `tenant_id`
(constraint #1); large-int columns are `BigInteger` (a Gmail `historyId` and an epoch-millisecond
`sort_ts` both exceed int32). There is no migration framework — the schema evolves via
`create_all` + the shared additive [`ensure_columns`](../reference/db.md) reconcile (ADR-0067).

| Table | Scope | Holds |
| --- | --- | --- |
| `mail_thread` | `(tenant_id, label, thread_id)` | One cached landing row — subject/sender/snippet/date, `unread`, `message_count`, and the `sort_ts` ordering key. |
| `mail_label` | `(tenant_id, label_id)` | The rail's folders + unread counts, in rail order. |
| `mail_sync` | `(tenant_id)` | The change cursor: Gmail `history_id`; IMAP `uid_validity`/`uid_next` reserved (all `BigInteger`). |
| `mail_landing` | `(tenant_id, label)` | Per-folder landing metadata: the page-1 `next_cursor` (so a cached view keeps its "Older") and when it was last full-synced. |

---

## Dependencies

| Service | Purpose |
| --- | --- |
| `core-app` | Platform API — token retrieval, event bus |
| `postgres` | The tenant-scoped local cache (`mail_*` tables, ADR-0096) |
| `nats` | Event publication (`mail.sent`) |
| Gmail API (`gmail.googleapis.com`) | The underlying mail provider |

---

## Run & extend

### Enable the module

The mail fragment is already included in the top-level `compose.yaml`.  Bring
it up with the rest of the stack:

```bash
task up          # or: docker compose up -d
```

### Connect your Google account

Before the module can access Gmail the operator must:

1. Connect a Google account from the web shell (**Settings → Connect**, or the
   **Connect** button on a Google-backed module card). The Gmail scopes are requested
   **automatically** (#241): mail declares them in its manifest (`oauth_scopes`), and the
   shell passes them at connect — Settings requests the union across every module, so one
   connect grants Calendar / Tasks / Gmail together. The scopes are
   `https://www.googleapis.com/auth/gmail.modify` (read + mark read/unread) and `…/gmail.send`
   (exported as `GMAIL_API_SCOPES` in `epicurus_mail.gmail`); the core adds the default identity
   scopes. **Upgrading from < v0.7.0:** an account connected when mail still requested
   `gmail.readonly` must **reconnect once** to grant `gmail.modify`, or the mark-read/unread
   tools return a reconnect hint (the core accumulates scopes, so reconnecting is non-destructive).

2. Ensure the Gmail API is enabled in the Google Cloud project associated with
   the OAuth client credentials (see [OAuth operator setup](../reference/oauth.md#operator-setup-google)).

### Adding a provider

1. Implement `MailProvider` in a new file (e.g. `imap.py`).
2. Add a settings field to select the provider (e.g. `MAIL_PROVIDER=imap`).
3. Instantiate the correct provider in `app.py` and pass it to `build_module`.
4. No changes to `service.py` or the tool surface are needed.

### Run locally (outside Docker)

```bash
# Start the data plane
task infra-up

# Run the mail service with dev defaults
uv run uvicorn epicurus_mail.app:app --reload --port 8087
```

The service will fail health-checks for Gmail tools until a Google account is
connected, but the HTTP surface (`/health`, `/manifest`, `/status`, `/resolve/…`,
`/messages/…`, `/pages/mailbox`) works immediately (the Gmail-backed reads return an
error hint until an account is connected).

### Adding a provider (updated for the `mailbox` page, ADR-0087)

Implementing `MailProvider` now also means the `mailbox`-page seam: `list_labels`,
`list_threads` (cursor-paginated), `get_thread`, `archive`, `trash`, and `get_attachment`
— typed in mail-domain terms. A backend that can't do one should **capability-gate** it
(e.g. return an empty label list, or raise a clear "unsupported") rather than force a fake
symmetry (ADR-0030): asymmetry is fine, silent wrong answers are not.
