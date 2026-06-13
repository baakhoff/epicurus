# Calendar module

## What it is

A provider-neutral calendar capability for the agent.  The module exposes three
MCP tools — list events, create an event, find free slots — and routes them
through a pluggable `CalendarProvider` interface.  Two providers ship in v0.1:

- **`LocalCalendarProvider`** — events stored in the shared Postgres database.
  Works with no external account; suitable for private task lists and
  scheduling within the platform.
- **`GoogleCalendarProvider`** — reads and creates events via the Google
  Calendar REST API.  Token is fetched from the core's OAuth vault (no secret
  ever touches this module); requires the tenant to have connected their Google
  account.

The domain model is provider-neutral: an `Event` is an `Event` regardless of
backend.  Adding a new provider (CalDAV, Microsoft Exchange, …) requires
implementing the `CalendarProvider` ABC; the tools and wire format stay
unchanged (ADR-0016).

## Contract

### MCP tools

| Tool | Description |
|------|-------------|
| `calendar_list_events(range_days=7)` | List events in the next *range_days* days (1–90). Returns a list of event objects ordered by start time. |
| `calendar_create_event(title, start, end, description?, location?)` | Create a new event. `start`/`end` are ISO-8601 strings. Returns the created event. |
| `calendar_find_free(duration_minutes=60, range_days=7)` | Find open time slots of at least *duration_minutes* in the next *range_days* days. Returns a list of `{start, end}` windows. |

All tools are provider-agnostic: swapping `CALENDAR_PROVIDER` in the
environment requires no tool call changes.

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

### REST endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Readiness probe (standard ops surface). |
| `GET` | `/metrics` | Prometheus metrics (standard ops surface). |
| `GET` | `/manifest` | Module manifest (tools, events, UI descriptor). |
| `GET` | `/status` | Live status: active provider, availability, event count (local only). |
| `POST /GET …` | `/mcp` | MCP SSE endpoint used by the core agent host. |

### NATS events

This module does not emit or consume NATS events in v0.1.

## Configuration

| Environment variable | Default | Description |
|----------------------|---------|-------------|
| `CALENDAR_PROVIDER` | `local` | Active provider: `"local"` or `"google"`. |
| `CALENDAR_GOOGLE_ID` | `primary` | Google Calendar ID (Google provider only). `"primary"` = the authenticated user's default calendar. |
| `DATABASE_URL` | `postgresql+asyncpg://epicurus:epicurus-dev@localhost:5432/epicurus` | Postgres DSN for the local provider's event store. |
| `PLATFORM_URL` | `http://localhost:8080` | Core service URL for OAuth token fetching (Google provider) and platform API calls. On the Docker network: `http://core-app:8080`. |
| `DEFAULT_TENANT_ID` | `local` | Tenant this instance serves. |
| `NATS_URL` | `nats://nats:4222` | NATS broker URL. |
| `LOG_LEVEL` | `info` | Structured-log level. |

### Using the Google provider

1. Ensure the core is configured with a Google OAuth client (see
   [OAuth service](../reference/oauth.md)).
2. Connect your Google account from **Settings → Connect Google** in the web
   shell. The Google OAuth app must have the
   `https://www.googleapis.com/auth/calendar` scope enabled.
3. Set `CALENDAR_PROVIDER=google` (and optionally `CALENDAR_GOOGLE_ID`) in
   your `.env`.  Restart the calendar service.

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

1. Implement `CalendarProvider` ABC in
   `services/calendar/src/epicurus_calendar/providers/`.
2. Register it in `app.py` (add an `elif settings.calendar_provider == "yourname"` branch).
3. Update the `CALENDAR_PROVIDER` enum in the `UiSection.config_schema` in
   `service.py`.
4. Add tests in `tests/`.

No tool or manifest changes are needed — the provider seam is real.
