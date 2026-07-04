# Calendar module

## What it is

A provider-neutral calendar capability for the agent.  The module exposes five
MCP tools — list, create, edit, and delete events, plus find free slots — and
routes them through a pluggable `CalendarProvider` interface.  Two providers ship:

- **`LocalCalendarProvider`** — events stored in the shared Postgres database.
  Works with no external account; this is the **silent default** that backs the
  module when nothing is connected (ADR-0030) and is never shown as a selectable
  provider.
- **`GoogleCalendarProvider`** — reads, creates, edits, and deletes events via the
  Google Calendar REST API.  Token is fetched from the core's OAuth vault (no secret
  ever touches this module); requires the tenant to have connected their Google
  account.

Since **v0.5** (ADR-0030) the module follows the **account/collection model**: it holds
*all* its backends at once and routes per the operator's selection, rather than a single
provider chosen at startup. The operator connects an account (Google), sees every calendar
it exposes, toggles which to show, and picks the one new events land on — all from the
core-rendered connected-accounts section. A `CollectionRouter` (`providers/router.py`) reads
the stored selection from the core and overlays the **enabled** calendars on read while
writing to the **active** one; with nothing enabled it falls back to local. When no active
calendar is set, writes prefer a **connected external calendar** — the first enabled one,
else a connected provider's own default (Google's `primary`) — and land in the silent local
store only when nothing external is connected (#433).

The domain model is provider-neutral: an `Event` is an `Event` regardless of
backend.  Adding a new provider (CalDAV, Microsoft Exchange, …) requires
implementing the `CalendarProvider` ABC; the tools and wire format stay
unchanged (ADR-0016).

Since **v0.2** the module also contributes a core-rendered **Calendar page** (month /
week / agenda) via the `calendar` archetype — it supplies the events, the shell draws the
views. Since **v0.6** that page is **editable** (#208): it declares create/edit/delete
*actions* that name the write tools, and the shell invokes them through the core's tool
proxy — no module markup (see *Calendar page* under Contract, below).

Since **v0.8** (#252, ADR-0037) the module supports **all-day events** end-to-end — fixing
the bug where all-day events rendered one day early — and lets the operator **choose which
calendar** a new event lands on, from a picker in the create form.

Since **v0.10** (#378) every event the page returns is **tagged with its calendar** —
`calendar_id`, the same `account:collection` token the create picker uses — so the shell can
group events by calendar and let the operator **toggle each calendar's visibility**; the shell
also caches each month to paint instantly on reopen (#379). The router sets `calendar_id` as it
overlays each enabled calendar, so the tag matches the calendars the operator can pick.

Since **v0.4** the module speaks the **entity-reference contract** (ADR-0019): listed events
come back as interactive chips, a referenced event resolves to a core **hover-card**, and the
module is a **chat-attachment source** so an event can be attached to a turn. It supplies data
only — the core renders the chip, the hover-card, and the panel (see *Entity references,
hover-cards & attachments* under Contract, below).

Since **v0.11** (#432, ADR-0075) events support **recurrence** (an RFC 5545 RRULE) and
**attendees** (a guest list). A recurring event is one stored *series* (the master, carrying
the rule) plus zero or more *exceptions* overriding a single occurrence (edited or deleted);
editing/deleting takes an `edit_scope` of `"this"` (one occurrence) or `"all"` (the whole
series) — see *Recurring events* under Contract, below. Since **v0.12** (#445) `edit_scope`
also takes `"following"` — this occurrence and every later one, splitting the series in two.
Since **v0.13** (#444) event creation can attach a **Google Meet** video-call link — see
*Google Meet*, below. Since **v0.14** (#471, ADR-0082) the create/edit **form** renders the
`recurrence` field as a **friendly repeat picker** (None / Daily / Weekdays / Weekly / Monthly
/ Yearly / Custom…) shared with the tasks form, instead of a raw RRULE text box — the picker
just authors the same RRULE string, so the agent tool surface and everything below the form is
unchanged (the `recurrence` parameter still takes a bare RRULE).

## Contract

### MCP tools

| Tool | Description |
|------|-------------|
| `calendar_list_events(range_days=7)` | List events in the next *range_days* days (1–90). Returns the matching events as **entity-reference chips** (ADR-0019), ordered by start time; its listing text truncates past `LIST_CAP` (50, ADR-0084/#468) with a "…and N more" note — the chips still carry every matching event. |
| `calendar_create_event(title, start, end, all_day=false, location?, description?, calendar_id?, recurrence?, attendees?, add_meet=false)` | Create a new event. `start`/`end` are ISO-8601 strings — a value **without a UTC offset is read in the operator's configured timezone** (ADR-0039, #433) — or **dates** (`YYYY-MM-DD`) when `all_day` is set (`end` = inclusive last date). Lands on the **write-default** calendar (active → first enabled external → connected provider's primary → local), or on `calendar_id` when given — an `account:collection` token (e.g. `google:primary`). `recurrence` (#432) is an RFC 5545 RRULE (e.g. `"FREQ=WEEKLY;COUNT=10"`, no `"RRULE:"` prefix) making this a recurring series; `attendees` is a comma-separated guest email list; `add_meet` (#444) attaches a Google Meet link — Google-only, a no-op on the local store. Returns the created event (`meet_url` set when a Meet link was attached). |
| `calendar_update_event(event_id, title?, start?, end?, all_day?, location?, description?, calendar_id?, recurrence?, attendees?, edit_scope="this")` | Edit an event. Only the fields passed change; the rest are left as-is (naive `start`/`end` follow the same operator-timezone rule as create). Pass `all_day` (with matching `start`/`end`) to switch between timed and all-day. Found and edited **wherever it lives** across the enabled calendars (#208); pass `calendar_id` — the event's own `account:collection` tag from a listing/page — to edit its home calendar directly instead of probing each one (#435). For a recurring event, `edit_scope` is `"this"` (#432, default — just the named occurrence), `"following"` (#445 — this occurrence and every later one, splitting the series in two), or `"all"` (#432 — the whole series); `recurrence` is only honoured with `edit_scope="all"` or `"following"` (raises with `"this"` — an instance can't carry its own rule; omitted with `"following"`, the new tail series just continues the existing pattern). Returns the updated event; raises if absent. |
| `calendar_delete_event(event_id, calendar_id?, edit_scope="this")` | Delete an event wherever it lives (`calendar_id` targets its home calendar directly, as in update). `edit_scope` mirrors update: `"this"` (#432) removes just the named occurrence, `"following"` (#445) removes it and every later occurrence (truncating the series), `"all"` (#432) removes the whole series. Returns `{deleted: true, id}`; raises if absent. |
| `calendar_find_free(duration_minutes=60, range_days=7)` | Find open time slots of at least *duration_minutes* in the next *range_days* days. Returns a list of `{start, end}` windows. |

All tools are provider-agnostic and route through the operator's selection (ADR-0030):
`calendar_list_events` overlays every enabled calendar (or local); `calendar_create_event`
writes to the **write default** (active, else the first enabled external calendar, else a
connected provider's primary, else local — #433) unless a `calendar_id` token picks another;
`calendar_update_event` / `calendar_delete_event` act on whichever enabled calendar holds the
event (a supplied home-calendar token first, then active → other enabled → local, the same
search `get_event` uses). A `calendar_id` token is decoded by the `CollectionRouter` into a
concrete `account:collection` target; the page's per-event Edit/Delete actions carry the
event's own token in their `args` (#435), so shell edits go straight to the owning calendar.

### Event object

```json
{
  "id": "string",
  "title": "string",
  "start": "2025-06-15T10:00:00+00:00",
  "end":   "2025-06-15T11:00:00+00:00",
  "all_day": false,
  "description": "string | null",
  "location":    "string | null",
  "provider": "local | google",
  "recurrence": "string | null",
  "recurring_event_id": "string | null",
  "attendees": [{"email": "string", "display_name": "string | null", "response_status": "string"}],
  "meet_url": "string | null"
}
```

`recurrence`/`recurring_event_id`/`attendees` are new in **v0.11** (#432) — see *Recurring
events*, below. `meet_url` is new in **v0.13** (#444) — see *Google Meet*, below.

**Timezones (#433, ADR-0039).** A timed `start`/`end` that carries a UTC offset is honoured
as that instant. A **naive** value (no offset — the common natural-language case, e.g. the
agent writing the operator's "3 PM" as `2026-07-02T15:00:00`) is read as wall time in the
**operator's configured timezone**, fetched from the core via
`PlatformClient.get_timezone()` per write. An unreachable core or an unknown zone name
degrades to UTC (the pre-#433 behaviour) rather than failing the write. The web create/edit
forms are unaffected — they always submit offset-carrying instants.

**All-day events** carry `all_day: true` and represent a *floating* date range: internally
`start`/`end` are UTC-midnight day boundaries with `end` **exclusive** (the day after the last
day, matching Google's all-day model — a single-day event spans one day). On the **page**
(below) they serialize as bare date strings (`"2026-06-15"`), never timed instants, so the
shell renders them on their calendar date with **no timezone conversion**. Treating an all-day
date as a UTC instant is what made events appear one day early for viewers behind UTC; the
floating-date contract fixes it end-to-end (ADR-0037, #252).

### Recurring events & attendees (#432, ADR-0075)

A recurring event is one **series** — the *master* row/object, carrying `recurrence` (an
RFC 5545 RRULE, e.g. `"FREQ=WEEKLY;COUNT=10"`) — plus zero or more **exceptions** overriding
a single occurrence (edited fields, a moved time, or deleted entirely). Listing/reading
returns individual **occurrences** (`recurring_event_id` set to the series' id;
`recurring_event_id` is `null` on the series object itself and on a one-off event).

- **Google** does the RRULE expansion server-side (`events.list(singleEvents=true)`,
  unchanged since before this feature) — the provider only passes `recurrence`/`attendees`
  through on writes and maps `recurringEventId` / `originalStartTime` on reads. Patching or
  deleting an *instance* id natively becomes a per-occurrence exception; no extra work needed.
- **Local** (no account) stores the master row plus exception rows (keyed by the occurrence's
  *original* — unmodified — start, encoded in the exception's own id as
  `<series-id>_<original-start>Z`) and expands the RRULE with `dateutil.rrule` on every read,
  bounded to the requested window (an unbounded `FREQ=DAILY` never iterates past the window).
  A *timed* series also stores the operator's configured **IANA timezone** at the moment
  `recurrence` is written (create, or an `edit_scope="all"` update that sets/redefines it) and
  expands the rule in that zone (#446, ADR-0077) — so a "9:00 AM" weekly event keeps its
  wall-clock hour across a DST change instead of drifting by the UTC-offset delta. All-day
  series ignore it (floating dates, ADR-0037); a legacy series with no stored zone falls back
  to the pre-fix UTC anchor. A moved occurrence is windowed by its *actual* (possibly moved)
  time, not its original slot, so rescheduling one across a view boundary can't make it go
  missing from, or leak into, the adjacent window. Every returned occurrence's `start`/`end`
  normalize to a UTC offset regardless of the series' anchor zone (#467) — the anchor zone
  governs *which* wall-clock slots the RRULE produces, not the wire representation of the
  result.

**Edit scope.** `calendar_update_event` / `calendar_delete_event` take `edit_scope`:
`"this"` (default) acts on just the named occurrence — for Google, PATCH/DELETE on its
instance id; for local, an exception row is created/updated (or a tombstone, for delete).
`"all"` acts on the whole series — resolved to the series' own id first if an instance id
was given (a lookup on Google; parsed from the id locally), then edited/deleted directly.
Setting `recurrence` requires `edit_scope="all"` or `"following"` — a single occurrence
can't carry its own rule (`edit_scope="this"` with `recurrence` set raises).

**"This and following"** (`edit_scope="following"`, #445) splits the series in two at the
named occurrence: the original series is truncated (an `UNTIL` set to the prior occurrence,
any `COUNT` dropped — it alone fully captures the new stopping point) so it ends just before
it, and that occurrence plus every later one move to a **new series** carrying the edit —
continuing the original cadence (a `COUNT`-bound rule is renumbered to just the remaining
occurrences; an `UNTIL`-bound or unbounded rule is unchanged) unless `recurrence` overrides
it outright. An occurrence already individually edited (`edit_scope="this"`) later in the
series keeps its own fields through the split — only the split point and genuinely
*unmodified* later occurrences take the new baseline. Splitting at a series' own **first**
occurrence has nothing "before" to keep separate, so it degrades to `"all"` — editing/deleting
the whole series in place, no split. Deleting `"following"` truncates the series the same way
and drops every occurrence from the split point on, including their own per-occurrence
overrides. On Google this is two calls — a PATCH truncating the original master's `recurrence`
plus (for an edit) a new `events.insert` for the tail — best-effort, since Google has no
cross-event transaction.

**Attendees** (`attendees`, a comma-separated email list on the tools) invites guests;
`response_status` (`needsAction` / `accepted` / `declined` / `tentative` — Google's
vocabulary, which is also iCalendar's PARTSTAT set) reflects live RSVP state on a
Google-backed event and starts `needsAction` for a newly invited local guest.

### Google Meet (#444)

`calendar_create_event`'s `add_meet` flag attaches a Google Meet video-call link at creation
time — **Google-only**; the local store has no conferencing backend to mirror it against, so
`add_meet` is silently a no-op there (the event is still created, just without a link). On
Google, the provider sends `conferenceData.createRequest` (a client-generated `requestId`,
Google's idempotency key for the conference sub-request, plus
`conferenceSolutionKey: {"type": "hangoutsMeet"}`) with `conferenceDataVersion=1` on the
`events.insert` call; Google provisions the conference and returns it inline in the same
response in the overwhelming majority of cases. The event's `meet_url` is read from
`conferenceData.entryPoints`, the one whose `entryPointType` is `"video"` — best-effort: on
the rare occasion a conference is still provisioning when the response comes back (no entry
points yet), `meet_url` reads `null` rather than retrying. There is no edit-time equivalent
(no `edit_scope` variant adds/removes a Meet link from an existing event) and no `add_meet`
field on `calendar_update_event`. The web create form always offers the toggle regardless of
which calendar is targeted (no cross-field conditional visibility) — its own description
notes it only takes effect on Google; `EventDetail` shows a "Join with Google Meet" link
when `meet_url` is set.

### Connected accounts & collections (ADR-0030)

The module declares `collections = {noun: "calendar", multi: true, providers: ["google"]}`
in its manifest and serves **`GET /accounts`**: one account per supported external provider,
each with `connected` (the live OAuth state) and, when connected, its `collections` (every
Google calendar — `{account, collection, title, writable, color?}`, primary first, with the
user's own Google calendar colour when set — the shell tints that calendar's events and menu
dot with it, #431). `local` is never listed — it is the silent default.

The core merges this with the operator's stored selection and serves it to the shell at
`GET /platform/v1/modules/calendar/collections`; the shell renders per-calendar on/off
toggles plus an active picker. Saving `PUT …/collections` persists
`{enabled: [CollectionRef], active: CollectionRef | null}`. The module reads that selection
via `PlatformClient.get_collections()` (a Postgres-only read at
`GET …/collections/prefs` — no round-trip back to the module) and routes:

- **read** (`calendar_list_events`, the page) overlays the **enabled** calendars; with none
  enabled it reads the local default;
- **write** (`calendar_create_event`, `calendar_find_free`) targets the **active** calendar;
  with none active it prefers a connected external calendar — the first **enabled** one, else
  a connected provider's default (`primary`) — and uses local only when nothing external is
  connected (#433). The New-event picker preselects the same default (Google lists the
  primary calendar first).

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
      "all_day": false,
      "location": "Room 4", "description": "Daily sync", "provider": "local",
      // Per-event actions (#208): Edit opens a prefilled form, Delete confirms.
      "actions": [
        { "tool": "calendar_update_event", "label": "Edit", "icon": "pencil",
          "form": true, "args": { "event_id": "e1" },
          "fields": ["title", "all_day", "start", "end", "location", "description",
                     "recurrence", "attendees"],
          "form_values": { "title": "Standup", "all_day": false, "start": "…", "end": "…",
                           "location": "Room 4", "description": "Daily sync",
                           "recurrence": "", "attendees": "" } },
        { "tool": "calendar_delete_event", "label": "Delete", "icon": "trash",
          "intent": "danger", "confirm": "Delete 'Standup'? This can't be undone.",
          "args": { "event_id": "e1" } }
      ]
    },
    // An all-day event serializes start/end as floating dates (end exclusive), not instants.
    { "id": "e2", "title": "Holiday", "start": "2026-06-18", "end": "2026-06-19",
      "all_day": true, "provider": "google", "actions": [ /* … */ ] },
    // A recurring occurrence (#432): edit/delete actions gain an `edit_scope` picker
    // ("This event" / "This and following events" / "All events", #445); Delete becomes a
    // form (its choice is the confirmation).
    { "id": "s1_20260622T090000Z", "title": "Team sync",
      "start": "2026-06-22T09:00:00+00:00", "end": "2026-06-22T09:30:00+00:00",
      "recurring_event_id": "s1", "provider": "google",
      "actions": [
        { "tool": "calendar_update_event", "label": "Edit", "form": true,
          "args": { "event_id": "s1_20260622T090000Z" },
          "fields": ["title", "all_day", "start", "end", "location", "description",
                     "recurrence", "attendees", "edit_scope"],
          "field_choices": { "edit_scope": [{ "value": "this", "label": "This event" },
                                            { "value": "following", "label": "This and following events" },
                                            { "value": "all", "label": "All events" }] } },
        { "tool": "calendar_delete_event", "label": "Delete", "intent": "danger", "form": true,
          "fields": ["edit_scope"], "form_values": { "edit_scope": "this" },
          "args": { "event_id": "s1_20260622T090000Z" },
          "confirm": "Delete 'Team sync'? This can't be undone." }
      ]
    }
  ],
  // Page-level action: "New event" opens a create form (time prefilled to the next hour).
  // The all-day toggle (declared via `date_toggle`) switches start/end to date pickers.
  // When more than one writable calendar exists, a labeled `calendar_id` picker is added.
  "actions": [
    { "tool": "calendar_create_event", "label": "New event", "icon": "plus",
      "intent": "primary", "form": true,
      "fields": ["title", "all_day", "start", "end", "location", "description", "calendar_id"],
      "form_values": { "all_day": false, "start": "…", "end": "…", "calendar_id": "local" },
      "field_choices": { "calendar_id": [ { "value": "local", "label": "Local" },
                                          { "value": "google:primary", "label": "Personal" } ] } }
  ]
}
```

The page overlays every **enabled** calendar (ADR-0030); `provider` is a label of the
sources actually present (e.g. `"local"`, `"google"`, or `"local, google"`), not a single
backend name.

**Editable (#208, ADR-0034).** The page declares actions in the **same vocabulary as the
`board` archetype** (ADR-0024): each names an MCP write tool, and the shell invokes it through the
core's tool proxy (`POST /platform/v1/modules/calendar/tools/{tool}`, validated against the
manifest), refetching the page on success. A page-level **New event** action opens a create
form; each event carries **Edit** (a prefilled form) and **Delete** (a confirm) action. The
shared core form renders `start`/`end` as native datetime pickers because the tools mark them
`format: date-time`. The module ships no markup — it supplies data + declares the actions.

**All-day toggle & calendar picker (#252, ADR-0037).** The create/edit forms carry an
**All day** switch: the `start`/`end` fields declare `date_toggle: "all_day"`, so the shared
SchemaForm collapses them to date pickers (emitting floating `YYYY-MM-DD` values) when it is
on. The **New event** action also offers a `calendar_id` picker — a labeled `field_choices`
select of the writable calendars (local + each connected, writable Google calendar) whose
values are `account:collection` tokens — so the operator chooses where a new event lands; it
is shown only when more than one writable calendar exists, and defaults to the active one.

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
| `GET` | `/status` | Live status: `google_connected` (best-effort), `google_timezone` (the Google Calendar's IANA timezone when connected, else `null` — read by the core's `now` tool, ADR-0039), and `local_events` (local store count). |
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
| `start_dt` | `timestamptz` | Start time (timezone-aware). For an all-day event, UTC midnight of the first day. |
| `end_dt` | `timestamptz` | End time (timezone-aware). For an all-day event, UTC midnight of the day after the last (exclusive). |
| `description` | `text` | Optional description. |
| `location` | `varchar(512)` | Optional location. |
| `all_day` | `boolean` | All-day (date-only) event flag. |
| `recurrence` | `text`, nullable | RFC 5545 RRULE on a series master; `NULL` on a plain event or an exception row (#432). |
| `recurring_event_id` | `varchar(64)`, nullable, indexed | The master's `event_id`, on an exception row only; `NULL` on a plain event or a master itself (#432). |
| `excluded` | `boolean` | Tombstones a single deleted occurrence; meaningless outside an exception row (#432). |
| `attendees` | `text`, nullable | JSON-encoded guest list (see `Attendee`); `NULL`/blank means no guests (#432). |
| `timezone` | `varchar(64)`, nullable | On a series master only: the IANA zone its RRULE expands in, captured from the operator's configured timezone whenever `recurrence` is written; `NULL` on a plain event, an exception, or a master written before this column existed (falls back to UTC expansion, #446). |
| `created_at` | `timestamptz` | Row insertion timestamp. |

Unique constraint: `(tenant, event_id)`.

`all_day`, `recurrence`, `recurring_event_id`, `excluded`, `attendees`, and `timezone` were all
added after the table's first release. There is no migration framework, so
`LocalEventStore.init` runs an additive `_ensure_columns` step that adds each in place on an
existing table (mirroring `TaskStore._ensure_columns`, #248); rows written before a given
column existed read `NULL`, coerced to the documented fallback above.

The **Google provider** stores no data locally; all state lives in Google
Calendar and in the core's OAuth vault. An all-day Google event uses `start.date`/`end.date`
(date-only) rather than `start.dateTime`/`end.dateTime`; the provider maps between those and
the `all_day` flag.

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
