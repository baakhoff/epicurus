# Reference: push notifications

`epicurus_core_app.push` (#670, ADR-0102) — VAPID-signed web push: per-device subscriptions,
per-category + quiet-hours preferences, and the send path. Core-owned, not a module (ADR-0018)
— there is no `push` service; every endpoint below lives on `core-app` at `/platform/v1/push`.

The flow is: a browser subscribes via the Push API → the core stores the subscription →
some caller (today, only the Settings "send test notification" button) calls
[`PushService.notify`](#pushservicenotify-core-internal) → the core resolves the tenant's
prefs (category/automation toggle → quiet hours → rate cap) → either delivers a VAPID-signed
push to every subscribed device, queues it for a quiet-hours digest, or skips it — and the
service worker (`services/web/src/sw.ts`) turns a delivered push into a system notification
and a deep link back into the PWA.

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
| `POST /test` | Send one real notification through the full pipeline (category defaults to `"system"`) — the manual-verification button; not a general send API (see below). Returns `{outcome, sent_count, pruned_count}`. |

## `PushPrefs`

One row per tenant (`PushPrefsStore`, table `push_prefs`) — the settings-primitives shape
(self-healing `init()`, unset falls back to a default; see `timezone_prefs.py`/ADR-0039).

| Field | Type | Meaning |
| --- | --- | --- |
| `categories` | `dict[str, ChannelPrefs]` | Per-category push/center toggle (§`ChannelPrefs` below). Unknown/unset category defaults to `{push: true, center: true}`. |
| `known_categories` | `[{id, label}]` | The platform-owned taxonomy (`system`, `chat`, `mail`, `calendar`, `tasks`, `automation`) — server-supplied so the UI never hardcodes it. |
| `quiet_hours_enabled` | `bool` | Whether the quiet window below is active. |
| `quiet_hours_start` / `quiet_hours_end` | `str` (`"HH:MM"`) | The quiet window in the tenant's configured timezone (ADR-0039). May wrap past midnight (e.g. `22:00`–`07:00`); a zero-width window (`start == end`) is treated as never-quiet. |

`ChannelPrefs = {push: bool, center: bool}` — shared with the notification center (#671):
`push` gates a browser push, `center` gates a durable row in that feature's `notifications`
table. Both exist on every category from this PR, even though `center` has no reader until
#671 ships (ADR-0102 §4).

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
the automations engine's push sink, a future system notice — never a module (ADR-0102 §5; if
a module ever needs to trigger a push, that gets a `PlatformClient` method and an endpoint
added in the PR that needs it, per the module-side-client-helper lesson, ADR-0020).

Resolves, in order: (1) the category/automation's effective `push` toggle (`PushPrefs.
effective`) — off returns `skipped_disabled`; (2) quiet hours in the tenant's timezone — inside
the window, the notification is queued (`push_queue`) and `queued` is returned, never dropped
(ADR-0102 §2); (3) an in-memory per-tenant rate cap (`PUSH_RATE_CAP_PER_HOUR`, default 30/hour,
0 = unlimited) — over the cap returns `skipped_rate_limited`; otherwise it fans out to every
subscribed device via VAPID-signed webpush and returns `sent` (with `sent_count`/
`pruned_count`). `NotifyResult.outcome` is one of `sent | queued | skipped_disabled |
skipped_rate_limited | skipped_no_devices`.

A subscription the push service reports **Gone** (404/410 — an uninstalled PWA, cleared site
data, an expired registration) is pruned automatically; that's expected churn, not an error.

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

## Service worker (`services/web/src/sw.ts`)

`push` — parses the JSON payload (`{title, body, category, deep_link, entity_ref}`) and calls
`self.registration.showNotification`; a second push in the same `category` replaces the
first in the OS tray (`tag`) rather than stacking. `notificationclick` — focuses an existing
PWA window and navigates it to `deep_link`, or opens a new one. Both are testable only under
`vite preview` (the injectManifest-built SW never runs under `vite dev`) — see
[web](../services/web.md).

See the running services that speak this contract: [core-app](../services/core-app.md#push-notifications-adr-0102)
(the send path) and [web](../services/web.md) (subscribe flow + settings UI + service worker).
