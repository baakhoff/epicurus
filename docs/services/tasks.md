# tasks — provider-neutral task management

**`epicurus-tasks`** is a sidecar module that manages tasks via a swappable provider
back-end (ADR-0016). The agent interacts with one stable tool surface regardless of
which provider is active. **v0.1 ships two providers:**

- **`local`** (default) — tasks stored in the module's own tenant-scoped Postgres
  table. Works with no external account.
- **`google`** — tasks in Google Tasks, via the Google Tasks REST API. Token is
  fetched from the core's OAuth vault; no credential lives in this module.

Post-v0.1: add Todoist, Microsoft To Do, or any other provider without reshaping the
tool surface. Host port **8091**.

**v0.2.0** adds a **Tasks** left-nav page — a core-rendered `board` of open tasks grouped
by due date, where the user completes, edits, and adds tasks — and the `tasks_update` tool
that backs editing (ADR-0018). The module supplies data only; the shell renders it.

**v0.3.0** makes the module a **chat-attachment source** (ADR-0019): a task can be picked in
the composer's attach menu and the agent uses it as explicit context for the turn. The module
serves the picker and resolve over its open tasks (see *Chat-attachment source*, below); the
core attach menu renders it.

**v0.4.0** makes agent-referenced tasks **entity-reference chips** (ADR-0019): `tasks_list`
returns its tasks as chips, hovering one shows the core **hover-card** (due date, status) and
clicking opens it in the right-panel `entity-detail` view. The module declares `resolver` and
serves `GET /resolve/task/{id}`; the core renders the chip, the hover-card, and the panel.

**v0.6.0** adopts the **account/collection model** (ADR-0030): there is no `TASKS_PROVIDER`
dropdown any more. The module holds both backends at once — the silent **local** default plus
**Google** — and routes to the task list the operator selects in the core-rendered
connected-accounts section. A `TasksRouter` (`router.py`) resolves the selection from the core's
stored prefs and falls back to local if the core is unreachable.

**v0.8.0** makes tasks **`multi`** — **each enabled list is a category** (ADR-0036, refining
ADR-0030's single-active for tasks). The board **aggregates open tasks across every enabled
list**, tagging each card with the list it came from; the **Add task** form offers a **list
picker** (the enabled writable lists, by name) so the operator chooses the category per task,
and each card's Complete / Edit routes back to the list the task belongs to. A single failing
list is skipped, not fatal (#209). Reads fall back to the local store only when no list is
enabled. The router stamps each task with its `list_id` / `list_title`; titles come from each
account's discovery (a lookup failure degrades to the list id, never failing the board).

**v0.9.0** lets the operator **pick a list when adding from chat and move tasks between
lists** (ADR-0038, #257). A new **`tasks_lists`** tool reports the available lists so the
agent can ask which one (it previously had no way to see them); `tasks_update` gains a
**`to_list_id`** move target, and the board's Edit form gains a **List** picker (shown with
≥2 writable lists). Google Tasks has no cross-list move API, so a move **recreates** the task
in the target list and deletes the source — it gets a new id, and subtasks/order aren't
carried. Moves operate between external lists (the local "Personal" store is the silent
default and only shows when no external list is enabled).

**v0.10.0** gives the board **view controls** (ADR-0049, #298): the operator can change how
tasks are laid out and surface completed ones. A **Group by** control switches the column
layout — **Due date** (default), **Status**, **Priority**, **List** (when there are named
lists), or **None** (a single flat list) — and a **Show** filter chooses the task scope —
**Open** (default), **Completed**, or **All**. The controls are declared in the board data
(`controls`) and rendered by the shell as a toolbar; selecting one re-fetches the page with a
forwarded query param (`group` / `show`), so grouping and filtering stay module-side with no
core change. Completed cards are struck through and offer **Reopen** in place of **Complete**.
The provider read seam gains a `scope` (`open` / `done` / `all`) so completed tasks can be
fetched (local filters the `completed` flag; Google sets `showCompleted`/`showHidden`).

**v0.11.0** adds **task deletion** (#336) and a tidier add affordance (#337). A new
**`tasks_delete`** tool (agent-usable) and a per-card **Delete** board action (operator,
confirm-gated) both route to the provider's existing `delete_task` via the `TasksRouter` — one
delete path, validated against the manifest. The bulky **Add task** toolbar button becomes a
compact icon-only **"+"** via a new `icon_only` board-action hint the shell renders (the label
moves to a tooltip + `aria-label`).

**v0.12.0** fixes `tasks_update` phantom updates (#475): an agent asked to clear a task's due
date could call `tasks_update` repeatedly, see success every time, and change nothing. Three
fixes. **Clear sentinel** — `due=""` and `notes=""` now explicitly unset the field (Google
receives a PATCH `null`; the local store writes `NULL`), distinct from omitting the field
(`None`), which still means "leave unchanged." **No-op rejection** — `tasks_update` with no
mutable field and no `to_list_id` raises an actionable error instead of silently succeeding (the
Google provider still GETs-and-returns on a field-less call at the provider layer — a safe
low-level fallback — but the tool the agent and board actually call never reaches it emptyhanded
now). **Cross-list resolution** — `complete_task` / `update_task` / `delete_task` with no
`list_id` now search the operator's lists (active → other enabled → local, the same order
`get_task` already used) via `TasksRouter._locate_task` instead of assuming the default write
target, so a mutation reaches a task that lives in a non-default list instead of 404ing there.

**v0.13.0** adds **creating a task list** (#474) — previously only possible outside epicurus, in
Google Tasks' own UI. A new **`create_list`** provider seam, a **`tasks_create_list(title)`**
MCP tool, and a board-level **New list** action (shown whenever the Add form's list picker is,
i.e. once an external account is connected — ADR-0031 auto-enables its lists on connect) all
route through the `TasksRouter` to the sole configured external provider — **Google only**: the
local store is a single implicit list by design (ADR-0030) and has no lists of its own to
create, so `LocalTasksProvider.create_list` raises `NotImplementedError` rather than pretending
to support it. The returned list id is immediately usable as `list_id` on `tasks_add` or
`to_list_id` on `tasks_update`, but — like any newly discovered Google list — it needs the
operator's one-time toggle in the connected-accounts Lists section before it appears as a board
category or in `tasks_lists`; the module has no path to write the operator's collection prefs
itself, so it cannot auto-enable what it just created (a natural follow-up if wanted). Renaming
and deleting a list are deliberately out of scope (destructive; need a policy for the tasks
inside).

**v0.14.0** adds **recurring tasks** (#471, ADR-0082) — on both providers, even though the
Google Tasks API **has no recurrence field** (repeat is UI-only). A task carries an optional
`repeat` rule (a bare RFC 5545 RRULE, e.g. `FREQ=WEEKLY`); **completing it materializes the next
instance** with the next due date and retires the rule on the completed one, so the recurrence
lives on exactly one open task at a time — re-completing can't double-fire and a `COUNT`/`UNTIL`
series ends cleanly. The rule is stored per provider — a `repeat` column on the local row, a
module-owned `task_repeats` side table keyed by task id for Google — but materialization is
provider-agnostic (it lives in the `TasksRouter.complete_task`). `tasks_add`/`tasks_update` gain
a `repeat` parameter (a `due` is required to anchor it, and the RRULE is validated at the tool
boundary); the board card shows a **Repeats weekly** badge and the hover-card a **Repeat** row.
The web form renders `repeat` as a **friendly repeat picker** (the shared `format: rrule` widget)
rather than a raw RRULE box — the agent tool still takes a raw RRULE. The next due date uses a
**skip-missed** policy: a task completed late rolls forward to the next *future* occurrence, not
an already-overdue one. Google caveats: the rule is invisible in Google's own UI; a task changed
directly in Google is reconciled on our next refresh; deleting it in Google retires the rule (GC
on miss). Materialization is **on-complete only** — a scheduled sweep for overdue-uncompleted
policies is a deliberate follow-up.

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Inputs | Returns |
| --- | --- | --- |
| `tasks_list(list_id?)` | `list_id`: optional list identifier (omit for default) | Open tasks as **entity-reference chips** (ADR-0019), newest first. |
| `tasks_lists()` | none | The available lists (categories) as `- <title> — id: <id>` text, so the agent can pick one (or report only the default list exists). |
| `tasks_create_list(title)` | `title`: the new list's display name | The created list as a `Collection` (`account`/`collection`/`title`/`writable`). **Google-only** (#474) — raises if no external account is connected; the local store has no lists of its own. |
| `tasks_add(title, notes?, due?, priority?, tags?, status?, list_id?, repeat?)` | `title`: required; rest optional. `list_id`: target list (from `tasks_lists`). `repeat`: RRULE making it recurring (needs a `due`) | The created `Task`. |
| `tasks_complete(task_id, list_id?)` | `task_id`: provider task ID; `list_id`: optional — omit to have it looked up across your lists | The updated `Task` with `completed=True`. A recurring task also spawns its next instance (ADR-0082). |
| `tasks_update(task_id, title?, notes?, due?, priority?, tags?, status?, list_id?, to_list_id?, repeat?)` | `task_id`: provider task ID; pass **at least one** mutable field or `to_list_id` — a field-less call raises. `due=""` / `notes=""` / `repeat=""` **clears** that field (`None`/omitted leaves it unchanged). `to_list_id`: **move** the task to this list; `repeat`: an RRULE (`""` makes it one-off) | The updated `Task` (the moved task on a move). |
| `tasks_delete(task_id, list_id?)` | `task_id`: provider task ID; `list_id`: optional — omit to have it looked up across your lists | A short confirmation string. **Permanent** — unlike `tasks_complete`, the task is removed. Idempotent on the local store (a missing id is a no-op). |

All tools are **provider-agnostic** (ADR-0030/0036). `tasks_list` with no `list_id`
**aggregates open tasks across every enabled list**; with a `list_id` it reads just that
list. `tasks_add` (a create) routes to the list named by `list_id`, or — with none — to the
default target (the active list, else the first enabled, else local). `tasks_complete` /
`tasks_update` / `tasks_delete` (mutations on an *existing* task) route to `list_id` when
given; with none, they **search** the same lists `get_task` does (active → other enabled →
local, #475) instead of assuming the default target, so a mutation still reaches a task that
lives in a different enabled list. Before adding from chat the agent should call
**`tasks_lists`** and ask which list when more than one exists. `tasks_update` edits content
(title/notes/due — `due=""` / `notes=""` clears one, #475) and rejects a call with nothing to
change; passing **`to_list_id`** moves the task (recreate+delete on Google — ADR-0038);
`tasks_complete` flips the done flag — distinct operations. The `Task` domain model is:

```python
class Task(BaseModel):
    id: str
    title: str
    notes: str | None = None
    due: str | None = None                                      # ISO date, e.g. "2025-01-15"
    status: Literal["open", "in_progress", "done"] = "open"
    completed_at: str | None = None
    priority: Literal["low", "medium", "high"] | None = None    # local-only
    tags: list[str] = []                                        # local-only
    repeat: str | None = None                                   # RRULE if recurring (#471, ADR-0082)
    list_id: str | None = None                                  # the list (category) — router-stamped
    list_title: str | None = None                               # its human label — router-stamped
    # `completed` is a computed alias: True when status == "done".
```

`list_id` / `list_title` are **not stored** — the router stamps them when it aggregates the
board so each card knows its category and routes its mutations to the owning list (ADR-0036).
`repeat` **is** persisted, but per provider: the local store keeps it in-row, and the Google
provider — Google Tasks has no recurrence field — keeps it in a module-owned `task_repeats`
side table keyed by task id (ADR-0082).

### HTTP

| Endpoint | Description |
| --- | --- |
| `GET /health` | Liveness probe. |
| `GET /metrics` | Prometheus metrics. |
| `GET /manifest` | Module manifest (tools, UI declaration, `collections` spec). |
| `GET /status` | `{"google_connected": bool}` (best-effort live OAuth check). |
| `GET /accounts` | Connected accounts + their task lists for the picker (ADR-0030). The core proxies + merges this at `GET /platform/v1/modules/tasks/collections`. |
| `GET /pages/{id}` | Page data for a manifest-declared page (`board`); the core proxies it (ADR-0018). Accepts forwarded `group` (due/status/priority/list/none) and `show` (open/done/all) query params (ADR-0049), each clamped to a known value. 404 for an unknown id. |
| `GET /attachments` | Chat-attachment picker (ADR-0019): open tasks as `{ref_id, kind, title}`. Core-proxied. |
| `GET /attachments/{ref_id}` | Resolve an attached task to `{title, excerpt}` (ADR-0019); missing task is `404`. Core-proxied. |
| `GET /resolve/{kind}/{ref_id}` | Hover-card resolver for a referenced task (ADR-0019); `kind` is `task`. Returns a `HoverCard`; unknown kind / missing task is `404`. Core-proxied. |
| `GET /mcp` (streamable-HTTP) | MCP tool surface (served by FastMCP). |

### Web UI (manifest, ADR-0007 Tier 1)

| Panel | What it shows / does |
| --- | --- |
| **Status** | Whether Google is connected (polled from `GET /status`). |
| **Lists** | Connected accounts + their task lists: per-list on/off toggles and a **default** picker for new tasks (ADR-0030/0036). |
| **Actions** | None — `tasks_list` returns entity-reference chips (surfaced in chat), so it is not a card-action button. |
| **Tasks page** | A left-nav `board` page (see below). |

### The Tasks page — `board` archetype (ADR-0018)

The module declares one page — `{id: "board", title: "Tasks", archetype: "board"}` — and
serves its data at `GET /pages/board`. The core renders it; the module ships **no markup**.

- **Columns** group the tasks **aggregated across every enabled list** by the operator's
  chosen **Group by** dimension (ADR-0049): **Due date** (default — Overdue / Today / Upcoming
  / No date), **Status**, **Priority**, **List** (one column per category), or **None** (a
  single flat list). Empty columns are dropped, and each card carries a **category tag** naming
  the list it came from (ADR-0036). Layout is a pure function,
  `build_tasks_board(tasks, today=…, group_by=…, scope=…, lists=…, default_list_id=…)`, so it
  is unit-tested without a clock — ISO date strings compare lexicographically, no parsing.
- **View controls** (`controls` in the board data) are a **Group by** selector and a **Show**
  filter (Open / Completed / All), rendered by the shell as a toolbar; changing one re-fetches
  the page with a forwarded query param (`group` / `show`, each clamped to a known value). The
  *Show* filter chooses the **scope** the providers read (`open` / `done` / `all`), so the
  operator can review completed work. Completing an open task removes it from the open view;
  in the Completed/All views a completed card is struck through (`done: true`) and offers
  **Reopen** (`tasks_update status=open`) in place of **Complete**.
- **Mutations are declarative actions** that name an MCP tool; the shell invokes it through
  the core (validated against the manifest) and refetches. Each card offers **Complete**
  (`tasks_complete`, one-tap), **Edit** (`tasks_update`, a form prefilled from the card), and
  **Delete** (`tasks_delete`, a `danger` action gated behind a confirm dialog, #336) — all
  carrying the task's `list_id` so the mutation routes to the **owning** list; the board
  offers **Add task** (`tasks_add`, a form) whose **list picker** chooses the target list
  (a labeled `field_choices` entry, value = list id → label = title). With **two or more**
  writable lists the Edit form also gains a **List** picker bound to `to_list_id` (prefilled
  to the task's current list); choosing another **moves** the task there — a recreate+delete
  on Google (ADR-0038). The **Add task** action sets `icon_only: true` so the shell renders it
  as a compact **"+"** with a tooltip label (#337). A board-level **New list** action
  (`tasks_create_list`, a form with a single `title` field) appears alongside it whenever the
  list picker does — Google-only (#474); creating one still needs the operator's one-time
  enable toggle before it shows up as a category (see the version note above). The board never
  carries credentials or business logic — it is data plus tool references.

### Connected accounts & collections (ADR-0030)

The module declares `collections = {noun: "list", multi: true, providers: ["google"]}` and
serves **`GET /accounts`**: one account per supported provider, `connected` from the live OAuth
state and, when connected, its task lists (`{account, collection, title, writable}`). `local`
is never listed — it is the silent default.

The core merges this with the stored selection at
`GET /platform/v1/modules/tasks/collections`; the shell renders per-list on/off toggles plus a
**default** picker, and `PUT …/collections` persists `{enabled, active}` (`active` is the default
write target). The module reads it via `PlatformClient.get_collections()` (a Postgres-only read
at `GET …/collections/prefs`) and, being **`multi`**, **aggregates the board across every enabled
list** while routing each write/mutation to the list named by `list_id` (or the default — active,
else first enabled, else local). If the core is unreachable it degrades to local (local-first).

### Entity references & hover-cards (ADR-0019)

`tasks_list` returns its open tasks as **entity-reference chips** rather than a bare list: each
chip carries the task id (`kind = "task"`, `module = "tasks"`), so the agent can refer to a task
later without re-listing. Hovering a chip fetches the task's **hover-card**; clicking opens it in
the right panel's `entity-detail` view. The module supplies data only — the core renders both.
(Because the list tool now returns a chip envelope rather than plain text, it is no longer a
module-card action button — tasks are surfaced through chat.)

**Resolver** (`resolver = true`) — `GET /resolve/task/{ref_id}` returns the uniform `HoverCard`
envelope (`title` · `description` · `details: [{label, value}]`): the task's notes as the
description, plus **Due** (when set) and **Status** (Open / Completed) detail rows. An unknown
`kind` or a missing task is a `404`. The core proxies it at
`GET /platform/v1/modules/tasks/resolve/{kind}/{ref_id}`. The hover-card carries no `href` —
clicking opens the in-app entity-detail panel, not an outbound URL.

### Chat-attachment source (ADR-0019)

`attachable = true` — a task can be attached to a turn so the agent uses its details as
explicit context, beyond anything it would list itself:

- **Picker** — `GET /attachments` lists up to 50 **open** tasks as
  `{ref_id, kind: "task", title}` rows the composer shows.
- **Resolve** — `GET /attachments/{ref_id}` returns `{title, excerpt}` — the task's title,
  due date, status, and notes — which the agent injects into the turn's context.

Both are proxied by the core at `GET /platform/v1/modules/tasks/attachments[/{ref_id}]`; a
missing task is a `404`. They use the active provider's `get_task`, so they behave identically
against the local and Google backends. The picker offers the **default list** only (the core
attach proxy forwards no list selector).

## Provider detail

### `local` provider

- Tasks stored in `tasks_local` (Postgres), scoped by `tenant_id`.
- `list_id` is ignored — single flat list per tenant.
- Works out of the box with no operator setup beyond a running Postgres instance.
- `create_list` raises `NotImplementedError` — there is no list concept to add to (#474).

### `google` provider

- Calls the Google Tasks REST API (`tasks.googleapis.com`).
- OAuth token fetched from `GET /platform/v1/oauth/google/token` — **no client
  secret or refresh token lives in this module** (ADR-0020 / non-negotiable #8).
- `create_list` calls `POST /users/@me/lists` (`tasklists.insert`, #474).
- Requires the Google account to be connected via the Settings screen (issue #86
  OAuth flow) before any tool call can succeed.
- `list_id` defaults to `@default` (the user's default Google task list).
- Additional scopes required: `https://www.googleapis.com/auth/tasks`
  (requested at connect time via the incremental-scopes mechanism, issue #102).

## Configuration

`TasksSettings` extends [`CoreSettings`](../reference/config.md). There is **no
`TASKS_PROVIDER`** any more (ADR-0030): the module always backs itself with the local store
and routes to the connected Google list the operator selects, which lives in the core
(`module_prefs`), not in service config.

| Env var | Default | Meaning |
| --- | --- | --- |
| `PLATFORM_URL` | `http://core-app:8080` | Core service URL for OAuth token, collection prefs, and platform API calls. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Postgres DSN for the local default store. |

## Data model

### Local provider

- **Postgres `tasks_local`** — tenant-scoped task store:

| Column | Type | Description |
| --- | --- | --- |
| `pk` | `INTEGER` | Auto-increment primary key. |
| `id` | `VARCHAR(255)` | UUID task identifier (indexed). |
| `tenant_id` | `VARCHAR(63)` | Tenant scope (indexed). |
| `title` | `VARCHAR(1024)` | Task title. |
| `notes` | `TEXT \| NULL` | Optional free-text notes. |
| `due` | `VARCHAR(64) \| NULL` | Optional ISO date string. |
| `completed` | `BOOLEAN` | Whether the task is done. |
| `completed_at` | `VARCHAR(64) \| NULL` | ISO timestamp when completed. |
| `created_at` | `DATETIME` | Auto-set at insert time. |
| `status` | `VARCHAR(32) \| NULL` | `open` / `in_progress` / `done` (added v0.5.0). |
| `priority` | `VARCHAR(16) \| NULL` | `low` / `medium` / `high`; local-only (added v0.5.0). |
| `tags` | `TEXT \| NULL` | JSON array of labels; local-only (added v0.5.0). |
| `repeat` | `TEXT \| NULL` | RFC 5545 RRULE if recurring; local-only (added v0.14.0, #471). |

Unique constraint on `(tenant_id, id)`. A read's `scope` selects the rows by the `completed` flag (ADR-0049): `open` (the default, `completed = FALSE`), `done` (`completed = TRUE`), or `all` (no filter); all ordered by `created_at DESC`. `tasks_list` always reads the `open` scope; the board's *Show* filter passes the chosen scope.

Schema is created automatically by `TaskStore.init()` at startup, which also **reconciles
columns added after the table's first release** — there is no migration framework, so `init()`
runs `create_all` and then `ALTER TABLE … ADD COLUMN` for any model column missing from an
existing table (additive only; the v0.5.0 `status`/`priority`/`tags` fields and the v0.14.0
`repeat` field). Without this, a database provisioned before v0.5.0 has no `status` column and
**every** task read (the board, `tasks_list`, the attachment picker, the resolver) 500s with
`column tasks_local.status does not exist` (#247). Destructive changes — drops, renames, type
changes, `NOT NULL` backfills — still require a real migration.

- **Postgres `task_repeats`** (added v0.14.0, #471, ADR-0082) — emulated recurrence rules for
  **external-provider** tasks (Google has no recurrence field). Tenant-scoped, keyed by
  `(tenant_id, list_id, task_id)`; the local store keeps its own rule in `tasks_local.repeat`
  and never uses this table:

| Column | Type | Description |
| --- | --- | --- |
| `pk` | `INTEGER` | Auto-increment primary key. |
| `tenant_id` | `VARCHAR(63)` | Tenant scope (indexed). |
| `list_id` | `VARCHAR(255)` | The provider list the task lives in (e.g. `@default`). |
| `task_id` | `VARCHAR(255)` | The provider task id (indexed). |
| `rrule` | `TEXT` | The bare RFC 5545 RRULE. |
| `created_at` | `DATETIME` | Auto-set at insert time. |

Unique constraint on `(tenant_id, list_id, task_id)`. Created by the same `TaskStore.init()`
`create_all` (it shares the module's SQLAlchemy metadata). A row is written on `add_task`/
`update_task`, filled onto reads, and **retired** on `delete_task` or a `get_task` 404 (GC on
miss). Writes are delete-then-insert so they work identically on SQLite (tests) and Postgres.

### Google provider

No task persistence — tasks live in Google Tasks. The OAuth token is stored in
OpenBao by the core's OAuth subsystem under `oauth/tokens/google` (tenant-scoped). A repeating
Google task's rule is the exception: it lives in the module's `task_repeats` table above
(Google Tasks has no recurrence field), keyed by the provider list + task id (ADR-0082).

## Dependencies

core-app (OAuth token endpoint) · Postgres (`local` provider + the `task_repeats` recurrence
side table) · NATS · `python-dateutil` (RRULE expansion for materialization, #471).

## Run & extend

```bash
# One container backs both local + Google; the operator picks the active list in the UI:
docker compose up -d tasks
```

To use Google: connect the account and pick a list in **Modules → Tasks → Lists** (or connect
from **Settings**). No restart or env change is needed (ADR-0030).

**Adding a new provider** — implement the `TasksProvider` Protocol (including `is_available`,
`list_collections`, and `create_list`) in a new file, add it to the `external` map in `app.py`
(keyed by its account id), and add it to `PROVIDER_LABELS` + `collections.providers` in
`service.py`. No tool or model changes are needed. If the new provider has no concept of
multiple lists, `create_list` can raise `NotImplementedError` like the local store does — the
router's own `create_list` only ever calls it on a genuine external provider, never on local.

Package `epicurus_tasks`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `Task` domain model (provider-neutral). |
| `providers.py` | `TasksProvider` Protocol — the swappable back-end seam (list (by `scope`)/add/complete/update/delete + `get_task` + `is_available`/`list_collections`/`create_list`). |
| `local_provider.py` | `LocalTasksProvider` — Postgres-backed task store (the silent default); `create_list` raises `NotImplementedError` (#474) — a single implicit list has nothing to create. |
| `google_provider.py` | `GoogleTasksProvider` — Google Tasks REST API (+ list-discovery + delete + `create_list` via `tasklists.insert`, #474); persists/fills/GCs emulated recurrence rules via an injected `RepeatStore` (#471, ADR-0082). |
| `router.py` | `TasksRouter` — routes ops to the operator's active list across local + Google (ADR-0030); moves a task between lists by recreate+delete (ADR-0038); `_locate_task` resolves an existing-task mutation across lists when `list_id` is omitted (#475); `create_list` routes to the sole configured external provider (#474); `complete_task` **materializes** a recurring task's next instance via `_materialize_next` (#471, ADR-0082). |
| `recurrence.py` | Pure RRULE math (#471, ADR-0082): `validate_rrule` (tool-boundary check) + `next_due` (the next occurrence, **skip-missed** policy), date-only (naive) parsing. |
| `db.py` | `TaskStore` — SQLAlchemy ORM + CRUD helpers (list/add/complete/update/get/delete) for the local store (incl. the `repeat` column); `RepeatStore` — the `task_repeats` side table for external-provider recurrence rules (#471). |
| `service.py` | MCP tools (`tasks_list`/`tasks_lists`/`tasks_create_list`/`tasks_add`/`tasks_complete`/`tasks_update`/`tasks_delete`, the last two taking `repeat`) + manifest UI (+ `collections` spec) + the Tasks `board` page (`PageSpec` + the pure `build_tasks_board` builder with group-by/scope **view controls**, the **New list** board action, the `repeat` form field + badge, and `coerce_group`/`coerce_scope`, ADR-0049) + entity-reference, hover-card & chat-attachment helpers + `tasks_accounts` (the `/accounts` view). |
| `app.py` | Lifespan, provider router wiring, `GET /status`, `GET /accounts`, `GET /pages/{id}`, `GET /attachments[/{ref_id}]`, `GET /resolve/{kind}/{ref_id}`, app factory. |
| `settings.py` | `TasksSettings` (adds `platform_url`, `database_url`). |
