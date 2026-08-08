# Reference: push notifications & the notification center

`epicurus_core_app.push` (#670, ADR-0102) and `epicurus_core_app.notifications` (#671,
ADR-0104) — VAPID-signed web push and the durable in-app record of every push-worthy event.
Both core-owned, not a module (ADR-0018) — there is no `push` or `notifications` service;
every endpoint below lives on `core-app` at `/platform/v1/push` or `/platform/v1/notifications`.

The flow is: a browser subscribes via the Push API → the core stores the subscription →
a category-based caller — the Settings "send test notification" button, or the
[automations engine's push sink](#automations-push-sink-723) (#723) — calls
[`PushService.notify`](#pushservicenotify-core-internal) → the core **records a
notification-center row unconditionally** (#797: the center is a superset of push — the
durable log of everything that fired), then resolves push delivery (the category/automation's
`push` toggle → quiet hours → rate cap) → either delivers a VAPID-signed push to every
subscribed device, queues it for a quiet-hours digest, or skips it — and the service worker
(`services/web/src/sw.ts`) turns a delivered push into a system notification and a deep link
back into the PWA. The center row lands **immediately**, regardless of what the push half
does — a quiet-hours-held, rate-capped, undeliverable, or outright-failed push is never
missing from the center. `push` means "*also* deliver to devices"; there is no toggle that
suppresses the center row (`center` survives on the wire for compatibility but delivery no
longer consults it — see [`PushPrefs`](#pushprefs)). Every gate that declines or defers a
push also [logs a distinct line](#delivery-diagnostics-797), and the most recent attempt is
readable at [`GET /status`](#delivery-diagnostics-797).

## HTTP — `/platform/v1/push` (browser-facing)

Every route resolves `tenant_id` from a query param, falling back to the default tenant —
the same convention as [`/platform/v1/timezone`](platform-api.md) and the other Settings
routes; there is no request body carrying a tenant.

| Method · Path | Purpose |
| --- | --- |
| `GET /vapid-public-key` | The tenant's `applicationServerKey`, base64url — generated on first call and persisted (see [VAPID keys](#vapid-keys-adr-0102-1)). |
| `GET /subscriptions` | List the tenant's subscribed devices (`SubscriptionView[]` — no keys, just `id`/`device_label`/`created_at`/`last_seen_at`). |
| `POST /subscriptions` | Register (or refresh) a device. Body `{endpoint, p256dh, auth, device_label?}` — upserts on `(tenant, endpoint)`. 400 if any of `endpoint`/`p256dh`/`auth` is blank. |
| `DELETE /subscriptions/{id}` | Unsubscribe a device. 404 unknown id. |
| `GET /prefs` | The tenant's [`PushPrefs`](#pushprefs) — `categories` always carries one entry per `known_categories`, defaulted, so the UI never merges client-side. |
| `PUT /prefs` | Partial update — send only the fields that changed. Body `{categories?, quiet_hours_enabled?, quiet_hours_start?, quiet_hours_end?}`. 400 on a malformed `HH:MM`. |
| `POST /test` | Send one real notification through the full pipeline (category defaults to `"system"`) — the manual-verification button; not a general send API (see below). Returns `{outcome, sent_count, pruned_count, failed_count}` — `outcome: "sent"` with `sent_count: 0` and `failed_count > 0` is a delivery that failed outright (#797). |
| `GET /status` | Delivery-state diagnostics (#797): `{device_count, last_attempt}` — see [Delivery diagnostics](#delivery-diagnostics-797). |

## `PushPrefs`

One row per tenant (`PushPrefsStore`, table `push_prefs`) — the settings-primitives shape
(self-healing `init()`, unset falls back to a default; see `timezone_prefs.py`/ADR-0039).

| Field | Type | Meaning |
| --- | --- | --- |
| `categories` | `dict[str, ChannelPrefs]` | Per-category channel pair (§`ChannelPrefs` below). Unknown/unset category defaults to `{push: true, center: true}`. |
| `known_categories` | `[{id, label}]` | The platform-owned taxonomy (`system`, `chat`, `mail`, `calendar`, `tasks`, `automation`) — server-supplied so the UI never hardcodes it. |
| `quiet_hours_enabled` | `bool` | Whether the quiet window below is active. |
| `quiet_hours_start` / `quiet_hours_end` | `str` (`"HH:MM"`) | The quiet window in the tenant's configured timezone (ADR-0039). May wrap past midnight (e.g. `22:00`–`07:00`); a zero-width window (`start == end`) is treated as never-quiet. |

`ChannelPrefs = {push: bool, center: bool}`. Since #797 (amending ADR-0102 §4/ADR-0104 §1)
**the center is a superset of push**: every notification that fires is recorded in the
`notifications` table unconditionally, and `push` decides whether devices are *also*
notified. `center` is **vestigial** — still accepted, stored, and returned for contract
compatibility (and an [event subscription](#eventsubscription) still reads "either flag on"
as "the alert is on"), but the send path no longer consults it; the settings UI writes it as
`true` to converge stored pre-#797 values. Anything that previously relied on
`center: false` to suppress rows is affected — that state no longer exists.

`automation_overrides: dict[str, ChannelPrefs]` also exists on the store (`PushPrefsStore.
set_automation_override`) for the automations engine's per-sink config — no HTTP route yet,
since nothing can configure it until that engine (#662-668) lands.

## `PushService.notify` (core-internal)

```python
async def notify(
    self, tenant: str, *, category: str, title: str, body: str,
    deep_link: str | None = None, entity_ref: dict[str, Any] | None = None,
    automation_id: str | None = None,
) -> NotifyResult
```

**Not an HTTP endpoint.** This is the contract a core-side caller codes against directly —
the settings UI's test button and the automations engine's push sink today, a future
core-originated system notice tomorrow — never a module (ADR-0102 §5; if a module ever needs
to trigger a push, that gets a `PlatformClient` method and an endpoint added in the PR that
needs it, per the module-side-client-helper lesson, ADR-0020).

Records a [notification-center row](#notification-center-671-adr-0104) first,
**unconditionally** (#797) — before, and independent of, every push-routing decision below.
Then resolves `PushPrefs.effective(category, automation_id)` for the push half: (1) if
`effective.push` is off, push delivery is skipped (`skipped_disabled` — the center row above
was still written); (2) quiet hours in the tenant's timezone — inside the window, the
notification is queued (`push_queue`) and `queued` is returned, never dropped (ADR-0102 §2);
(3) an in-memory per-tenant rate cap (`PUSH_RATE_CAP_PER_HOUR`, default 30/hour, 0 =
unlimited) — over the cap returns `skipped_rate_limited`; otherwise it fans out to every
subscribed device via VAPID-signed webpush and returns `sent` (with `sent_count`/
`pruned_count`/`failed_count`). `NotifyResult.outcome` is one of `sent | queued |
skipped_disabled | skipped_rate_limited | skipped_no_devices` — and describes **push delivery
only**; the center row exists in every case. `sent` means the send path ran, not that
devices were reached: `sent_count: 0` with `failed_count > 0` is a delivery that failed on
every device.

`NotifyResult.notification_id` (#723) carries the center row's id back to the caller — set
the moment that row is written, so it rides along with every outcome above and is **always
set** since #797. A caller that wants to reference what was recorded — the automations push
sink builds an `EntityRef` from it — reads this instead of making a second, racy lookup;
`send_digest` (below) always leaves it `None`, since a digest summarizes several
already-recorded rows rather than writing one of its own.

A subscription the push service reports **Gone** (404/410 — an uninstalled PWA, cleared site
data, an expired registration) is pruned automatically; that's expected churn, not an error
(logged as `pruned dead push subscription`, and counted in `pruned_count` rather than
`failed_count`).

The outgoing wire payload's `body` is capped at `_MAX_PUSH_BODY_CHARS` (500 characters, an
ellipsis appended when trimmed) — a browser push service enforces its own size ceiling, and an
automation's full report should not be able to blow it. Only the JSON handed to `webpush()` is
shortened; the notification-center row written above always keeps the caller's complete text,
since a push payload limit is not a reason to lose part of the durable record.

## Delivery diagnostics (#797)

A push has to clear several independent gates to reach a device — the event was emitted, an
[event subscription](#event-alerts-732-adr-0114) exists for the pair (or a category caller
fired), push is on, quiet hours aren't holding it, the rate caps have budget, a device is
registered, and the push service accepted the delivery. Each declining/deferring gate logs a
**distinct, greppable line**, so silence is always attributable:

| Gate | Log line (verbatim) | Level |
| --- | --- | --- |
| No event subscription for `(module, event_type)` | `event alert declined: no subscription for this event` | info |
| Per-subscription event-alert rate cap | `event alert rate cap reached` | warning |
| Push disabled for the category/automation | `push skipped: push disabled for this notification` | info |
| Quiet hours active | `push queued for quiet-hours digest` | info |
| Tenant-wide push rate cap | `push rate cap reached; delivery skipped` | warning |
| No registered devices | `push skipped: no registered devices` | warning |
| Delivery to the push service failed | `push send failed` | warning |
| Dead subscription pruned (404/410) | `pruned dead push subscription` | info |

Every line carries `tenant` (and `category`, or `module`/`type` for the event-alert pair).
The first two log per recorded event — the same volume as the intake's own `event recorded`
line. Note the two rate caps differ deliberately: the event-alert cap declines the whole
notification (center row included — a firehose into the center is still a firehose), while
the tenant-wide push cap only skips delivery (the center row is already written).

**`GET /platform/v1/push/status`** returns the same story without logs:

```json
{
  "device_count": 2,
  "last_attempt": {
    "at": "2026-08-08T10:12:00+00:00",
    "category": "system",
    "outcome": "sent",
    "sent_count": 2,
    "failed_count": 0,
    "pruned_count": 0
  }
}
```

`last_attempt` is the most recent call that actually *wanted* push delivery — a send
(however it went per-device), a quiet-hours queue, a rate-cap skip, or a no-devices skip; a
`skipped_disabled` never overwrites it (push was off, nothing was attempted). Held
**in-memory per tenant** (single-instance v1, the same disposable-cache trade as the rate
windows): `null` means "none since the core started", not "never". The settings card renders
this as its delivery-state readout, warns when push is enabled anywhere with
`device_count: 0`, and refreshes it after every "send test notification" click.

## VAPID keys (ADR-0102 §1)

Generated lazily, per tenant, on first send (or the first `GET /vapid-public-key` call) —
`{private_key: <PEM>, public_key: <base64url>}` stored in OpenBao at the tenant-scoped path
`push/vapid` (see [secrets](secrets.md)). No operator provisioning step: a VAPID key has no
external identity to prove (unlike an OAuth client secret), so there is nothing for an
operator to supply.

## Quiet-hours digest (ADR-0102 §2)

`PushDigestScheduler` (`push/queue.py`) is a plain poll loop — the same shape as
`ScheduledTurnScheduler`/`MaintenanceOrchestrator` — controlled by `PUSH_QUIET_POLL_INTERVAL_S`
(default 60s). Each tick, for every tenant with rows in `push_queue`, it checks whether that
tenant's quiet window has ended; if so, it sends **one** summary push ("N notifications while
you were quiet", deep-linking to `/notifications`) via `PushService.send_digest` and clears
the queue. A failed send leaves the queue intact for the next tick.

## Events (NATS)

`push.sent` — a best-effort usage/telemetry event published after every delivery attempt
(never gates the send; mirrors `llm.usage`'s "must never break the caller" posture). Scoped
`<tenant>.push.sent`.

| Field | Type | Meaning |
| --- | --- | --- |
| `tenant` | `str` | Owning tenant. |
| `category` | `str` | The notification's category (or `"digest"` for a quiet-hours digest). |
| `device_count` | `int` | Devices actually sent to (excludes pruned/failed). |

## Notification center (#671, ADR-0104)

The durable, category-filterable record of **every notification that fires** (#797 — a
superset of push: rows are written unconditionally, whatever the push half then does) —
written only by [`PushService.notify`](#pushservicenotify-core-internal) (there is no create
route: the center has exactly one writer). A core page (`/notifications`, ADR-0018/0019),
not a module page — the web shell's Settings-adjacent surfaces pattern, same as Push
notifications above.

### HTTP — `/platform/v1/notifications` (browser-facing)

| Method · Path | Purpose |
| --- | --- |
| `GET ""` | List the tenant's notifications, newest first. Query params: `category?` (filter to one category), `unread_only?` (bool, default false). |
| `GET /unread-count` | `{count}` — the shell badge's poll target (15s interval, matching `useAwayFinishedWatch`'s #492 precedent; not SSE, ADR-0104 §4). |
| `POST /{id}/read` | Mark one notification read (idempotent). 404 unknown id. |
| `POST /read-all` | Mark every unread notification read. Returns `{marked: <count>}`. |

### `Notification`

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | `str` | Opaque external id. |
| `category` | `str` | Same taxonomy as [`PushPrefs.known_categories`](#pushprefs) — reused, not duplicated. |
| `title` / `body` | `str` | The notification's text. |
| `deep_link` | `str \| None` | An in-app path to navigate to on click, rendered via `CardLink` (in-app `Link` / external new-tab / unsafe-scheme-dropped, the same handling a hover-card's `href` gets). Independent of `entity_ref` — a notification may carry either, both, or neither (ADR-0104 §5). |
| `entity_ref` | `EntityRef \| None` | ADR-0019's contract, rendered via `EntityRefChip` — no parallel rendering path. |
| `automation_id` | `str \| None` | Set when the automations engine's sink triggered this notification. |
| `created_at` | `str` (ISO) | When it was recorded. |
| `read_at` | `str \| None` (ISO) | `None` until marked read. |

### Retention

A per-tenant row cap (`NotificationStore`'s `max_per_tenant`, default 500), not time-based —
the oldest rows are pruned past the cap on every `create()` (ADR-0104 §3; contrast with the
module-event log's day-based `EVENTS_RETENTION_DAYS`, ADR-0103 §5 — a different retention
question: "what haven't I looked at" bounds naturally by count, not by age).

## Automations push sink (#723)

`epicurus_core_app.automations.push_sink` — the `push` sink's handler
(`docs/reference/automations.md#sinks`), the last of the ADR-0105 sink vocabulary's four to
get one. Before this, every one of the ten starter templates #717 shipped with
`sinks=["push"]` ran, recorded a ledger row, and delivered nothing an operator could see —
`SinkDispatcher._handlers.get("push")` returned `None`, so the sink was always "unavailable."

`make_push_sink(push_service)` builds a plain closure and goes **through**
[`PushService.notify`](#pushservicenotify-core-internal), never around it — quiet hours, the
tenant-wide rate cap, and the push/center toggles all apply exactly as for any other caller.

| Decision | What it does |
| --- | --- |
| **Category** | Always `"automation"` — one settings-UI toggle row covers every automation by default. |
| **Per-automation override** | `automation_id=automation.id` is passed on every call, so an operator can silence one noisy automation via `PushPrefs.automation_overrides` without muting the whole category (the ADR-0102 §4 seam, unused until now). |
| **Title** | The automation's own name (truncated defensively to 200 characters), the same "the entity's own label leads" preference [event alerts](#the-listener-eventalertlistener-core-internal) uses. |
| **Body** | The run's raw output, untouched — `notify()`'s wire-payload truncation (above) is what shortens what actually goes out over the wire; the center row keeps it in full. Blank output falls back to a fixed placeholder rather than an empty notification. |
| **Deep link** | `/observability?tab=runs&automation=<id>` — the same cross-link the Automations page's own per-row run history already uses, landing the operator on this automation's filtered run history rather than the flat automations list. |
| **`entity_ref`** | Not set — a plain notification (title + body + deep link only; ADR-0104 §5 explicitly supports this combination). |

**Artifact.** `notify()` always returns a `notification_id` (#797 — the center records every
notification that fires), and the handler returns an `EntityRef` — `ref_id=notification_id`,
`module="core"`, `kind="notification"`, `title=<the push title>` — which `SinkDispatcher`
collects onto the run's `automation_runs.artifacts`
(`docs/reference/automations.md#the-run-ledger`), so the runs feed renders a chip for it
exactly as it already does for a notes/kb document. `core` has no hover-card resolver, so the
chip falls back to its own title on hover — the same precedent `CoreEventEmitter._file_ref`
already sets for the Files page (`docs/services/core-app.md`). A per-automation override can
still silence this sink's *push*; the row — and therefore the artifact — is always there.

## Event alerts (#732, ADR-0114)

`epicurus_core_app.push.event_subscriptions` + `epicurus_core_app.push.event_alerts` —
"push me when X happens" for any module-declared event, with no automation involved. A
tenant opts in to individual `(module, event_type)` pairs; an enabled subscription becomes a
notification the moment a matching event is recorded, via
[`PushService.notify_effective`](#pushservicenotify_effective-core-internal).

**Not an automation.** No agent turn, no trigger queue, no ledger entry, no autonomy dial —
a dumb fan-out wired beside the automations engine, at the same `EventIntake.on_event` seam
`AutomationMatcher` uses (`docs/reference/automations.md`). Want reasoning or actions instead
of a tap on the shoulder? That's what an automation's event trigger is for. An automation
with its own trigger on the same event and an enabled alert both fire on the same occurrence
— **two notifications, by design**; nothing here dedupes them (deduping would mean the alert
has to know about every automation's trigger, which is exactly the coupling a dumb fan-out
avoids).

### HTTP — `/platform/v1/push` (browser-facing, same router as above)

| Method · Path | Purpose |
| --- | --- |
| `GET /event-subscriptions` | The tenant's *non-default* subscriptions only (`EventSubscriptionView[]`) — everything else is off. The settings UI unions this with every module's declared `events_emitted` (`GET /platform/v1/modules`), the same way it unions `/prefs`' sparse `categories` with `known_categories`. |
| `PUT /event-subscriptions` | Upsert one `(module, event_type)` row. Body `{module, event_type, push, center}` — every field required (unlike `/prefs`, there is nothing to merge against). Setting both `push` and `center` false deletes the row rather than storing an all-off one. 400 if `module`/`event_type` is blank. |

### `EventSubscription`

| Field | Type | Meaning |
| --- | --- | --- |
| `module` | `str` | The emitting module (`mail`, `tasks`, …), or a custom value the operator typed in. |
| `event_type` | `str` | The event's dotted type (`mail.received`) — exactly what `LoggedEvent.type` carries and what the automations trigger picker renders (manifest `events_emitted[].subject`, `events.` prefix stripped). |
| `push` / `center` | `bool` | This subscription's own [`ChannelPrefs`](#pushprefs) — independent of `PushPrefs.categories`; there is no category resolution in this path. Since #797: **either flag on means the alert is on** (and an alert that fires always lands in the center); `push` alone decides device delivery. |

Default is **no row**, unlike `PushPrefs.categories` (default on/on) — `EventSubscriptionStore.
get` returns `None` for an unsubscribed event, and the listener below treats `None` as "do
nothing." (`EventSubscriptionStore`, table `event_subscriptions`, unique on `(tenant, module,
event_type)`.)

### The listener (`EventAlertListener`, core-internal)

Wired to `EventIntake.on_event` beside `AutomationMatcher`. For every newly-recorded event:
look up a subscription for `(tenant, module, type)`; if none, or both channels are off, stop
— logging `event alert declined: no subscription for this event` (#797; the issue's original
bare return, the likeliest and least visible reason a notification never fires). Otherwise
check a per-subscription rate cap (`EVENT_ALERTS_RATE_CAP_PER_HOUR`, default 20/hour, 0 =
unlimited) — independent of, and in addition to, `PushService`'s own tenant-wide
`PUSH_RATE_CAP_PER_HOUR`; a single chatty event type must not be able to spend a tenant's
entire hourly push budget. The cap gates the whole notification (push and center together),
not push alone, and logs `event alert rate cap reached` when it declines. Fires regardless of
`causation_id` — unlike the automations matcher's loop guard, there is no agent turn here to
spiral, so an event an automation's own run produced is exactly as alert-worthy as one a
module emitted.

The notification's title/body render generically — no per-module knowledge: the entity's own
title leads when the event carries an `entity_ref` (rendered client-side as a hover-card
chip, ADR-0019), with `"<module> · <type>"` as the body; with no entity, `"<module> · <type>"`
is the title and the body is empty.

### `PushService.notify_effective` (core-internal)

```python
async def notify_effective(
    self, tenant: str, *, effective: ChannelPrefs, category: str, title: str, body: str,
    deep_link: str | None = None, entity_ref: dict[str, Any] | None = None,
) -> NotifyResult
```

The same send path as [`notify`](#pushservicenotify-core-internal) — unconditional center
write, then push routes to delivered now / queued for quiet hours / skipped — for a caller
that has already resolved its channels itself (only `effective.push` is consulted, #797).
`notify`'s `PushPrefs.effective(category, automation_id)` step is the *only* thing this
skips; quiet hours are still tenant-wide, so this still reads `PushPrefs` for that half.
`category` here is a label only (the notification center's filter key, and the `push.sent`
usage event's `category`) — it plays no role in resolving whether to send, unlike in
`notify`.

### Settings UI (`EventAlertsCard`)

Grouped by module, one row per declared event — plus a "Custom" section for any subscribed
`(module, event_type)` that isn't in the declared catalog (a free-text add form, the same
escape hatch the automations trigger picker offers). Since #797 each row renders two
**coupled** switches over the unchanged wire contract: **Alert** (on iff either stored flag
is on; enabling writes `{push: true, center: true}`, disabling writes both false) and
**Push** (`push`; turning it on enables the alert with it, turning it off leaves a
center-only `{push: false, center: true}`). Turning the alert off removes the row the same
way `PUT .../event-subscriptions` with both channels false removes it — there is no separate
delete endpoint.

## Service worker (`services/web/src/sw.ts`)

`push` — parses the JSON payload (`{title, body, category, deep_link, entity_ref}`) and calls
`self.registration.showNotification`; a second push in the same `category` replaces the
first in the OS tray (`tag`) rather than stacking. `notificationclick` — focuses an existing
PWA window and navigates it to `deep_link`, or opens a new one. Both are testable only under
`vite preview` (the injectManifest-built SW never runs under `vite dev`) — see
[web](../services/web.md).

See the running services that speak this contract: [core-app](../services/core-app.md#push-notifications-adr-0102)
(the send path + the notification center + event alerts + the automations push sink) and
[web](../services/web.md) (subscribe flow + settings UI + service worker + the Notifications
page). The automations engine itself, including the other three sinks, is documented at
[automations](automations.md).
