# Reference: `events`

Two layers, and it matters which one you want:

- **`epicurus_core.events`** — the async NATS client (the event *backbone*). Any service
  publishing or subscribing to anything uses this.
- **`epicurus_core.module_events`** — the module event *spine* (ADR-0103): one standardized
  envelope for announcing that the world changed, plus the durable log the core keeps of
  every announcement. **This is what a module emits.** Jump to
  [the spine](#the-module-event-spine) and [the event catalog](#the-event-catalog).

Subjects are tenant-scoped via [`scope_subject`](tenancy.md).

## `EventBus`

```python
class EventBus:
    def __init__(
        self,
        url: str = "nats://localhost:4222",
        *,
        user: str | None = None,
        password: str | None = None,
    ) -> None
    @classmethod
    def from_settings(cls, settings: CoreSettings) -> EventBus
```

Use it as an async context manager (`async with EventBus(...) as bus:`) or call
`connect()` / `close()` yourself.

`user` / `password` authenticate the connection (the bus requires it — #50, ADR-0066).
They are optional: both `None` connects **anonymously**, which keeps the bus usable
against an un-authenticated server (e.g. the integration testcontainers).
`from_settings` reads them from `NATS_USER` / `NATS_PASSWORD` — in the stack the core
authenticates as `core` and modules as `module`. See [NATS](../infrastructure/nats.md).

### Methods

| Method | Description |
| --- | --- |
| `async connect() -> None` | Connect to NATS. |
| `async close() -> None` | Drain and disconnect. |
| `async publish(subject, data, tenant_id=None) -> None` | Publish `data` to the tenant-scoped `subject`. |
| `async request(subject, data, *, timeout=2.0, tenant_id=None) -> Event` | Request/reply; await one response. |
| `async subscribe(subject, handler, *, tenant_id=None, queue="") -> Subscription` | Call `handler(Event)` per message. |
| `async subscribe_any_tenant(subject, handler, *, queue="") -> Subscription` | Call `handler(Event)` per message on `*.<subject>` — **every** tenant. Core-only. |
| `async reply(subject, replier, *, tenant_id=None, queue="") -> Subscription` | Respond to each request with `replier(Event)`'s result. |
| `client` *(property)* | The underlying NATS client; raises if not connected. |

`data` may be `bytes`, `str`, or a JSON-serializable `dict`. A non-empty `queue`
joins a queue group for load-balanced delivery.

### Failure behavior

- A **handler** passed to `subscribe` that raises is logged (with traceback)
  and skipped — the subscription keeps delivering subsequent messages.
- A **replier** passed to `reply` that raises is logged and sends **no
  response**: the requester times out, and the subscription keeps serving
  subsequent requests. Look for the failure in the replier's logs.
- Connection drops, reconnects, and client errors are logged
  (`nats disconnected` / `nats reconnected` / `nats client error`).

### Types

```python
Payload = bytes | str | dict[str, Any]
EventHandler = Callable[[Event], Awaitable[None]]
Replier = Callable[[Event], Awaitable[Payload]]
```

## `Event`

A received message (frozen dataclass):

| Member | Type | Description |
| --- | --- | --- |
| `subject` | `str` | The fully-scoped subject. |
| `data` | `bytes` | The raw payload. |
| `text` *(property)* | `str` | `data` decoded as UTF-8. |
| `json()` | `Any` | `data` parsed as JSON. |

### Example

```python
from epicurus_core import Event, EventBus

async def on_msg(event: Event) -> None:
    print(event.subject, event.json())

async with EventBus(settings.nats_url) as bus:
    await bus.subscribe("inbox.message", on_msg, tenant_id="local")
    await bus.publish("inbox.message", {"text": "hi"}, tenant_id="local")
```

### `subscribe_any_tenant` — core-only

Subscribes `*.<subject>`: the single-token wildcard sits exactly where `scope_subject`
puts the tenant, so it matches every tenant's copy of a subject and nothing else
(application subjects always carry that leading token).

**Do not use this from a module.** On the bus the core authenticates with unrestricted
pub/sub while a module is confined to its own tenant-scoped subjects
([ADR-0066](../infrastructure/nats.md)); a module reaching across tenants is a boundary
violation, not a feature. The core needs it because a per-tenant subscription list means a
tenant added at runtime is silently unheard until restart.

The handler must read the tenant off the message (`Event.subject`'s first token) or its
payload — and should trust neither without checking the other agrees.

---

## The module event spine

`epicurus_core.module_events` (ADR-0103). A module **emits** when something changed in the
world it owns: mail arrived, a calendar event moved, a note was saved. It says only that
the change happened and where to look — never what should be done about it. That is the
automations engine's job, and keeping the two apart is what lets a module emit before any
consumer exists, and lets a consumer be written once against every module.

### `emit_event`

```python
async def emit_event(
    bus: EventBus,
    *,
    tenant_id: str,
    module: str,
    event_type: str,
    dedup_key: str,
    payload: dict[str, Any] | None = None,
    entity_ref: EntityRef | None = None,
    occurred_at: datetime | None = None,
) -> EventEnvelope
```

The one way a module emits:

```python
from epicurus_core import EntityRef, emit_event

await emit_event(
    bus,
    tenant_id=tenant,
    module="mail",
    event_type="mail.received",
    dedup_key=f"gmail:{msg.id}",              # deterministic per change — never a uuid
    payload={"message_id": msg.id, "subject": msg.subject, "unread": 1},
    entity_ref=EntityRef(ref_id=msg.id, module="mail", kind="message", title=msg.subject),
)
```

`event_type` maps to the envelope's `type` field (the parameter avoids shadowing the
builtin at every call site). `occurred_at` defaults to now (UTC) — pass it explicitly when
the change happened earlier than the moment you noticed it, since that is what a digest
window and the feed order by.

Raises `ValueError` (via the envelope's validators) **before anything reaches the bus** on
a malformed type, a mismatched module prefix, an oversized payload, or a credential-shaped
payload key. Publishing is fire-and-forget: it returns once the client accepts the
message, not once anything consumes it.

### `EventEnvelope`

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | `int` | Envelope schema (currently `1`). Bumped only on a breaking shape change; additive optional fields do not bump it. |
| `tenant_id` | `str` | Constraint #1. Must be a well-formed tenant id. |
| `module` | `str` | The emitting module — one lowercase subject token. |
| `type` | `str` | Dotted, and **prefixed with `module`**: `mail.received`, `echo.pinged`. Also the subject suffix. |
| `occurred_at` | `datetime` | When the change happened, in the emitter's clock. Must be timezone-aware. |
| `dedup_key` | `str` | The emitter's idempotency key for the *change*. 1–255 chars. |
| `entity_ref` | `EntityRef \| None` | The entity this is about ([ADR-0019](../services/core-app.md)) — a feed row or notification renders a hover-card chip from it with no per-module code. |
| `payload` | `dict[str, Any]` | Pointers + minimal metadata. Capped and credential-screened. |

Helpers: `envelope.subject()` and `event_subject(type)` both return the *base* subject
(`events.<type>`); the bus tenant-scopes it at publish time. `EVENTS_WILDCARD`
(`"events.>"`) is what the core's intake subscribes.

### Two rules that will bite you

**`dedup_key` must be deterministic per change.** The core's log dedups on
`(tenant, module, dedup_key)`, so a poll loop that re-sees the same mail must reuse the
same key — a provider id (`"gmail:18f2c1"`) beats a fresh uuid, which defeats the whole
mechanism by being different every time. (`echo.pinged` is the one legitimate exception:
two pings genuinely *are* two changes, so its identity is fresh by definition.)

**The payload is pointers, never content — and this is enforced.** An id, a subject line,
a count. Never a mail body, never document text, never a credential. A consumer that needs
the real thing fetches it through the owning module's tools under its own authorization,
which is what keeps the log from becoming a second, unguarded copy of every module's data.
Two validators enforce it rather than ask:

- **`MAX_PAYLOAD_BYTES` = 4096.** Sized to fit ids and counts and to *not* fit a body.
- **Credential-shaped keys are rejected.** The [redaction](#redaction) rule matches key
  *names* by blunt case-insensitive substring, so a payload key containing `key`, `token`,
  `auth`, or `secret` is refused **even when innocent** — `idempotency_key` and `sort_key`
  fail exactly like `api_key`. Name a field for what it points at (`message_id`, not
  `message_key`), and never repeat an envelope field in the payload (`dedup_key` itself
  trips the screen).

### Redaction

`epicurus_core.redaction` — one rule, shared by every surface that shows operator-facing
data: the log console redacts on it (ADR-0031), the envelope *rejects* on it at emit, and
the events feed redacts on it again on the way out.

| Member | Description |
| --- | --- |
| `REDACTED_KEYS` | The substrings that mark a key name as credential-shaped. |
| `is_secret_key(key)` | Whether a key name looks like it holds a credential. |
| `secret_keys_in(mapping)` | The offending key names, sorted — for a "reject this" error. |
| `redact_mapping(mapping)` | A copy without its credential-shaped keys. |

Matching is a deliberate over-match: a false positive costs one hidden field or a rename, a
false negative leaks a credential to a browser tab, and those are not symmetric.

### Delivery posture

**Best-effort, at-most-once.** Core NATS pub/sub — an event emitted while the core is down
is *gone*, not queued. JetStream is enabled on the server and deliberately unused here
(ADR-0103 §4). This is why the core's `module_events` table, not the bus, is the copy of
record: "what happened" is a question you ask Postgres. Promoting the spine to JetStream is
a named follow-up; `dedup_key` already makes redelivery safe, so it is a transport change
rather than a contract change.

### The durable log

The core subscribes `*.events.>` (one subscription, every tenant), verifies that the
subject's tenant and the envelope's `tenant_id` agree, and records each event in the
tenant-scoped `module_events` table. Duplicates — same `(tenant, module, dedup_key)` — are
stored once; **first write wins**, since a later delivery of an already-recorded change
carries no newer truth. Retention is time-based (`EVENTS_RETENTION_DAYS`, default 30 days;
`0` disables). Malformed and mis-tenanted messages are logged and dropped.

Read it over HTTP — `GET /platform/v1/events` and `GET /platform/v1/events/stream` — see
[platform-api](platform-api.md). The Observability screen's **Events** tab is the live tail.

---

## The event catalog

Every event on the spine. **Adding an emitter? Add a row here** — this table is what an
operator (and the agent, via the indexed docs) reads to know what the system can tell them.

| Event | Module | Payload | `dedup_key` | Notes |
| --- | --- | --- | --- | --- |
| `echo.pinged` | `echo` | `{note?: str}` — a short crumb, truncated to 200 chars | fresh per ping, or caller-supplied | The reference emitter ([echo](../services/echo.md)). Fired by the `echo_ping` tool / the "Ping the spine" action. Carries an `EntityRef` of kind `ping`. Pass an explicit `dedup_key` to demonstrate the log's idempotency: two pings, one event. |
| `mail.received` | `mail` | `{message_id, from, subject (≤200 chars), folder, has_attachments: bool, provider}` | the provider message id | The first provider-backed emitter ([mail](../services/mail.md)). Fired from the cache reconcile (ADR-0096, #623) once per genuinely-new message — **never** on an initial or full resync (no-firehose). Carries an `EntityRef` of kind `message`. See *Provider caveats* below. |
| `mail.sent` | `mail` | `{to (≤200 chars), subject (≤200 chars)}` | the provider's sent message id | Fired after a confirmed send succeeds (`POST /send` / `POST /pages/mailbox/send`, ADR-0085) — best-effort, a spine hiccup never fails a completed send. Carries an `EntityRef` of kind `message`. |
| `mail.sync_failed` | `mail` | `{reason: "provider_error" \| "cursor_expired", provider}` | `"<reason>:<ISO timestamp>"` | Fired on a reconcile failure — a provider/auth error, or an expired sync cursor forcing a full resync. Rate-limited per running instance (`MAIL_SYNC_FAILED_COOLDOWN_S`, default 900s) so a flapping account can't storm the bus; each emission that clears the cooldown is a fresh observation, not a repeat, so the key is time-based rather than derived from the failure itself. |
| `calendar.event_created` | `calendar` | `{title (≤200 chars), start, end, all_day: bool}` | `"<provider>:<event_id>"` | Fired by `CollectionRouter.create_event` ([calendar](../services/calendar.md)) — the **provider-write** seam only; see *Provider caveats* below. Carries an `EntityRef` of kind `event`. |
| `calendar.event_updated` | `calendar` | `{title, start, end, all_day, time_changed: bool}` | `"<provider>:<event_id>:<change hash>"` | Fired by `CollectionRouter.update_event`. `time_changed` is a real before/after comparison (the router fetches the prior state first), not inferred from which args were passed — except in the narrow case the prior state can't be resolved, where it degrades to "were `start`/`end` actually supplied." The change hash means a genuinely different edit is its own log entry; an identical retried write dedups. |
| `calendar.event_cancelled` | `calendar` | `{title (≤200 chars)}` | `"<provider>:<event_id>"` | Fired by `CollectionRouter.delete_event`, after the delete actually succeeds (the event's prior state is snapshotted first — nothing is left to read once it's gone). |
| `calendar.event_starting_soon` | `calendar` | `{title, start, end, lead_minutes: int}` | `"<provider>:<event_id>:starting_soon"` | Fired by the lead-time scheduler (`epicurus_calendar.scheduler`, calendar's first periodic background job) when an event enters its lead window (tenant setting, default 15 minutes). Fire-once via a durable marker — proven to survive a restart. |
| `calendar.event_ended` | `calendar` | `{title, start, end}` | `"<provider>:<event_id>:ended"` | Fired by the same scheduler once an event's end time has passed; bounded lookback (`DEFAULT_LOOKBACK_MINUTES` = 60) — an event that ended longer ago than that before the scheduler could tick (e.g. downtime) is never reported. Fire-once, same marker mechanism as `event_starting_soon`. |
| `tasks.task_created` | `tasks` | `{title (≤200 chars), due, status}` | `"<account>:<task_id>"` | Fired by `TasksRouter.add_task` ([tasks](../services/tasks.md)). Carries an `EntityRef` of kind `task`. **Not** fired for a recurring task's auto-materialized successor — see *Provider caveats* below. |
| `tasks.task_completed` | `tasks` | `{title, due, status: "done"}` | `"<account>:<task_id>"` | Fired by `TasksRouter.complete_task`. |
| `tasks.task_updated` | `tasks` | `{title, due, status}` | `"<account>:<task_id>:<change hash>"` | Fired by `TasksRouter.update_task` for a non-move edit; same dedup posture as `calendar.event_updated`. |
| `tasks.task_moved` | `tasks` | `{title, due, status, from_list, to_list}` | `"<target account>:<new task_id>"` | Fired by the cross-list move seam (ADR-0038/#257) instead of `task_updated`, never alongside it — Google Tasks has no move API, so a move recreates the task in the target list and deletes the source, and the dedup key follows the *new* id. |
| `tasks.task_due_soon` | `tasks` | `{title, due, lead_days: int}` | `"<account>:<task_id>:due_soon"` | Fired by the lead-time scheduler (`epicurus_tasks.scheduler`, tasks' first periodic background job) when an open task enters its lead window (tenant setting, default 1 day) — evaluated against the *operator's local calendar day* (ADR-0039), reusing the same clock the overdue-recurrence sweep resolves. Fire-once via a durable marker. |
| `tasks.task_overdue` | `tasks` | `{title, due}` | `"<account>:<task_id>:overdue"` | Fired by the same scheduler once an open task's due date has passed. Fire-once, same marker mechanism as `task_due_soon`. |

Real module emitters (notes, knowledge, files) are companion issues and append their rows as
they land.

### Provider caveats

Notes on emitters whose truth depends on an external provider — polling granularity,
missing change feeds, provider-side dedup quirks.

**Mail / Gmail.** `changed_threads_since` (Gmail's `users.history.list`) reports most changes at
**thread** granularity, but its `messagesAdded` history-record type already carries the specific
message id, which is what `mail.received`'s message-level detection is built on — it is not
inferred from a thread-level diff. IMAP does not exist in this codebase yet (only `GmailProvider`
is implemented, despite the `MailProvider` seam being provider-neutral by design); a future IMAP
provider would derive new-message ids from the UIDs above the last-seen `UIDNEXT` on a
`UID FETCH`, and a `UIDVALIDITY` rotation would report through the same "cursor too old to
replay" path Gmail's expired-history 404 already uses — `mail.sync_failed(reason="cursor_expired")`
needs no provider-specific handling on the reconcile side.

**Calendar.** `event_created`/`event_updated`/`event_cancelled` only observe the **provider-write**
seam (`CollectionRouter`) — a change made directly in Google Calendar's own UI, outside epicurus,
is never seen and never emits, because calendar has no sync/reconcile layer analogous to mail's
(ADR-0096, #623). `invitation_received` / `attendee_responded` (Google-only, per ADR-0030 — the
local provider would stay silent by design) are **not implemented**: both would need that same
kind of polling/diff layer to detect a change nobody made through this module, which is a
materially larger feature than wiring emission into an existing seam. Declaring either without
actually publishing it would repeat mail's own "declared but never emitted" mistake (see
[mail](../services/mail.md)) rather than avoid it, so neither is in the manifest's `emits()` list
either. A future sync layer for calendar (mirroring mail's) is the natural place to add them.

**Tasks.** A recurring task's auto-materialized successor (the overdue sweep and on-complete
materialization, `TasksRouter._materialize`, ADR-0082) calls the *inner* provider's `add_task`
directly, bypassing the router's own `add_task` — so it does not currently emit `task_created`.
A deliberate scope limit for #664, not an oversight: folding emission into the materialize path
touches the same recurrence machinery a `#533`-class concurrency bug already lives in, and was
judged safer to leave alone than to touch in the same PR that first wires up event emission.
