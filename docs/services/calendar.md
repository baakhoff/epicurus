# Calendar module

## What it is

A provider-neutral calendar capability for the agent.  The module exposes three
MCP tools — list events, create an event, find free slots — and routes them
through a pluggable `CalendarProvider` interface.  Two providers ship:

- **`LocalCalendarProvider`** — events stored in the shared Postgres database.
  Works with no external account; this is the **silent default** that backs the
  module when nothing is connected (ADR-0030) and is never shown as a selectable
  provider.
- **`GoogleCalendarProvider`** — reads and creates events via the Google
  Calendar REST API.  Token is fetched from the core's OAuth vault (no secret
  ever touches this module); requires the tenant to have connected their Google
  account.

Since **v0.5** (ADR-0030) the module follows the **account/collection model**: it holds
*all* its backends at once and routes per the operator's selection, rather than a single
provider chosen at startup. The operator connects an account (Google), sees every calendar
it exposes, toggles which to show, and picks the one new events land on — all from the
core-rendered connected-accounts section. A `CollectionRouter` (`providers/router.py`) reads
the stored selection from the core and overlays the **enabled** calendars on read while
writing to the **active** one; with nothing enabled it falls back to local.

The domain model is provider-neutral: an `Event` is an `Event` regardless of
backend.  Adding a new provider (CalDAV, Microsoft Exchange, …) requires
implementing the `CalendarProvider` ABC; the tools and wire format stay
unchanged (ADR-0016).

Since **v0.2** the module also contributes a core-rendered **Calendar page** (month /
week / agenda) via the `calendar` archetype — it supplies the events, the shell draws the
views (see *Calendar page* under Contract, below).

Since **v0.4** the module speaks the **entity-reference contract** (ADR-0019): listed events
come back as interactive chips, a referenced event resolves to a core **hover-card**, and the
module is a **chat-attachment source** so an event can be attached to a turn. It supplies data
only — the core renders the chip, the hover-card, and the panel (see *Entity references,
hover-cards & attachments* under Contract, below).

## Contract

### MCP tools

| Tool | Description |
|------|-------------|
| `calendar_list_events(range_days=7)` | List events in the next *range_days* days (1–90). Returns the matching events as **entity-reference chips** (ADR-0019), ordered by start time. |
| `calendar_create_event(title, start, end, description?, location?)` | Create a new event. `start`/`end` are ISO-8601 strings. Returns the created event. |
| `calendar_find_free(duration_minutes=60, range_days=7)` | Find open time slots of at least *duration_minutes* in the next *range_days* days. Returns a list of `{start, end}` windows. |

All tools are provider-agnostic and route through the operator's selection (ADR-0030):
`calendar_list_events` overlays every enabled calendar (or local), `calendar_create_event`
writes to the active one (or local). An optional `list`/calendar override is not exposed on
the tools — the active calendar governs writes.

### Event object

```json
{
  "id": "string",
  "title": "string",
  "start": "2025-06-15T10:00:00+00:00",
  "end":   "2025-06-15T11:00:00+00:00",
  "description": "string | null",
  "location":    "string | null",
  "provider": "local | google"
}
```

### Connected accounts & collections (ADR-0030)

The module declares `collections = {noun: "calendar", multi: true, providers: ["google"]}`
in its manifest and serves **`GET /accounts`**: one account per supported external provider,
each with `connected` (the live OAuth state) and, when connected, its `collections` (every
Google calendar — `{account, collection, title, writable}`). `local` is never listed — it is
the silent default.

The core merges this with the operator's stored selection and serves it to the shell at
`GET /platform/v1/modules/calendar/collections`; the shell renders per-calendar on/off
toggles plus an active picker. Saving `PUT …/collections` persists
`{enabled: [CollectionRef], active: CollectionRef | null}`. The module reads that selection
via `PlatformClient.get_collections()` (a Postgres-only read at
`GET …/collections/prefs` — no round-trip back to the module) and routes:

- **read** (`calendar_list_events`, the page) overlays the **enabled** calendars; with none
  enabled it reads the local default;
- **write** (`calendar_create_event`) targets the **active** calendar; with none active it
  writes local.

If the core is briefly unreachable, the router degrades to the local default rather than
failing (local-first).

### Calendar page (`calendar` archetype — ADR-0018)

The module contributes a **Calendar** left-nav page. It supplies *data only* — a window of
events — and names the core `calendar` archetype, which renders the month / week / agenda
views; the module ships no markup. The page is served at `GET /pages/calendar` and proxied
by the core at `GET /platform/v1/modules/calendar/pages/calendar`.

`start` and `end` (ISO-8601) bound the window the shell is viewing; the core forwards them
to the module. When absent, the page falls back to the current month. An over-wide window
is clamped (≤ 92 days); `end ≤ start` or an unparseable bound returns `400`.

```jsonc
{
  "title": "Calendar",
  "provider": "local",
  "range": { "start": "2026-06-01T00:00:00+00:00",
             "end":   "2026-07-01T00:00:00+00:00" },
  "events": [
    { "id": "e1", "title": "Standup",
      "start": "2026-06-15T09:00:00+00:00",
      "end":   "2026-06-15T09:30:00+00:00",
      "location": "Room 4", "description": "Daily sync", "provider": "local" }
  ]
}
```

The page overlays every **enabled** calendar (ADR-0030); `provider` is a label of the
sources actually present (e.g. `"local"`, `"google"`, or `"local, google"`), not a single
backend name.

Read-first in v0.2 (view + navigate); creating and editing events from the page is a later
bump — the `calendar_*` MCP tools remain the agent's write path.

### Entity references, hover-cards & attachments (ADR-0019)

`calendar_list_events` returns its events as **entity-reference chips** rather than a bare list:
each chip carries the event id (`kind = "event"`, `module = "calendar"`), so the agent can refer
to an event later without re-listing. Hovering a chip fetches the event's **hover-card**; clicking
opens it in the right panel's `entity-detail` view. The module supplies data only — the core
renders both. (Because the list tool now returns a chip envelope rather than plain text, it is no
longer a module-card action button — events are surfaced through chat.)

**Resolver** (`resolver = true`) — `GET /resolve/event/{ref_id}` returns the uniform `HoverCard`
envelope (`title` · `description` · `details: [{label, value}]`): *When* (start–end), *Location*
(when set), and *Calendar* (the active provider). An unknown `kind` or a missing event is a `404`.
The core proxies it at `GET /platform/v1/modules/calendar/resolve/{kind}/{ref_id}`.

**Attachment source** (`attachable = true`) — an event can be attached to a turn:

- **Picker** — `GET /attachments` lists up to 50 **upcoming** events (next 30 days) as
  `{ref_id, kind: "event", title}` rows the composer shows.
- **Resolve** — `GET /attachments/{ref_id}` returns `{title, excerpt}` — the event's title, time,
  location, and description — which the agent injects into the turn's context.

Both are proxied by the core at `GET /platform/v1/modules/calendar/attachments[/{ref_id}]`; a
missing event is a `404`. All three surfaces route through the `CollectionRouter`'s `get_event`,
which searches the active calendar, then the other enabled calendars, then local — so a
referenced event resolves wherever it lives.

### REST endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Readiness probe (standard ops surface). |
| `GET` | `/metrics` | Prometheus metrics (standard ops surface). |
| `GET` | `/manifest` | Module manifest (tools, events, UI descriptor, `collections` spec). |
| `GET` | `/status` | Live status: `google_connected` (best-effort) and `local_events` (local store count). |
| `GET` | `/accounts` | Connected accounts + their calendars for the picker (ADR-0030). The core proxies + merges this at `GET /platform/v1/modules/calendar/collections`. |
| `GET` | `/pages/{page_id}` | Calendar archetype page data (ADR-0018). Accepts `start`/`end` (ISO-8601) query params bounding the window; defaults to the current month. The core proxies this — the shell never calls it directly. |
| `GET` | `/resolve/{kind}/{ref_id}` | Hover-card resolver for a referenced event (ADR-0019); `kind` is `event`. Returns a `HoverCard`; unknown kind / missing event is `404`. Core-proxied. |
| `GET` | `/attachments` | Chat-attachment picker (ADR-0019): upcoming events as `{ref_id, kind, title}`. Core-proxied. |
| `GET` | `/attachments/{ref_id}` | Resolve an attached event to `{title, excerpt}` (ADR-0019); missing event is `404`. Core-proxied. |
| `POST /GET …` | `/mcp` | MCP SSE endpoint used by the core agent host. |

### NATS events

This module does not emit or consume NATS events in v0.1.

## Configuration

There is **no provider-selection env var** (ADR-0030): the module always backs itself with
the local store and routes to connected Google calendars per the operator's selection, which
lives in the core (`module_prefs`), not in service config.

| Environment variable | Default | Description |
|----------------------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus` | Postgres DSN for the local default event store. |
| `PLATFORM_URL` | `http://localhost:8080` | Core service URL for OAuth token fetching (Google provider) and platform API calls. On the Docker network: `http://core-app:8080`. |
| `DEFAULT_TENANT_ID` | `local` | Tenant this instance serves. |
| `NATS_URL` | `nats://nats:4222` | NATS broker URL. |
| `LOG_LEVEL` | `info` | Structured-log level. |

### Using Google calendars

1. Ensure the core is configured with a Google OAuth client (see
   [OAuth service](../reference/oauth.md)). The OAuth app must have the
   `https://www.googleapis.com/auth/calendar` scope enabled.
2. In the web shell open **Modules → Calendar → Calendars** and press **Connect**
   (or connect Google from **Settings**). No restart and no env change are needed.
3. Toggle on the calendars you want shown and pick the one new events should land on
   (the active calendar). Selections persist per tenant in the core.

The calendar module never holds a client secret or refresh token — it fetches a
valid access token from the core's OAuth vault on each API call.

## Data model

The **local provider** owns the `calendar_events` table in the shared Postgres
database.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `integer` PK | Auto-increment surrogate key. |
| `tenant` | `varchar(63)` | Tenant scope (indexed). |
| `event_id` | `varchar(64)` | UUID stable identifier exposed to callers. |
| `title` | `varchar(512)` | Event title. |
| `start_dt` | `timestamptz` | Start time (timezone-aware). |
| `end_dt` | `timestamptz` | End time (timezone-aware). |
| `description` | `text` | Optional description. |
| `location` | `varchar(512)` | Optional location. |
| `created_at` | `timestamptz` | Row insertion timestamp. |

Unique constraint: `(tenant, event_id)`.

The **Google provider** stores no data locally; all state lives in Google
Calendar and in the core's OAuth vault.

## Dependencies

| Service | Purpose |
|---------|---------|
| Postgres | Local provider event store; schema auto-created on startup. |
| NATS | Event bus (heartbeat / future events). |
| `core-app` (platform API) | OAuth token fetch (Google provider). Discovery / MCP host. |

The Google provider additionally talks outbound to `https://www.googleapis.com/calendar/v3`.

## Run & extend

### Run locally

```bash
# Install deps (run from repo root)
uv sync

# Start with the local provider (default)
DATABASE_URL=postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus \
PLATFORM_URL=http://localhost:8082 \
  uv run python -m epicurus_calendar
```

The service is now reachable at `http://localhost:8080`.

### Add a new provider

1. Implement the `CalendarProvider` ABC (including `is_available` and `list_collections`)
   in `services/calendar/src/epicurus_calendar/providers/`.
2. Add it to the `external` provider map in `app.py` (keyed by its account id) and to
   `PROVIDER_LABELS` + the `collections.providers` list in `service.py` so it appears in the
   connected-accounts picker.
3. Add tests in `tests/`.

No tool or manifest-shape changes are needed — the provider seam and the account/collection
model carry the new backend (ADR-0016 / ADR-0030).
