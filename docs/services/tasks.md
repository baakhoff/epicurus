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

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Inputs | Returns |
| --- | --- | --- |
| `tasks_list(list_id?)` | `list_id`: optional list identifier (omit for default) | Open tasks as **entity-reference chips** (ADR-0019), newest first. |
| `tasks_lists()` | none | The available lists (categories) as `- <title> — id: <id>` text, so the agent can pick one (or report only the default list exists). |
| `tasks_add(title, notes?, due?, priority?, tags?, status?, list_id?)` | `title`: required; rest optional. `list_id`: target list (from `tasks_lists`) | The created `Task`. |
| `tasks_complete(task_id, list_id?)` | `task_id`: provider task ID; `list_id`: optional | The updated `Task` with `completed=True`. |
| `tasks_update(task_id, title?, notes?, due?, priority?, tags?, status?, list_id?, to_list_id?)` | `task_id`: provider task ID; only the fields passed change. `to_list_id`: **move** the task to this list | The updated `Task` (the moved task on a move). |

All tools are **provider-agnostic** (ADR-0030/0036). `tasks_list` with no `list_id`
**aggregates open tasks across every enabled list**; with a `list_id` it reads just that
list. `tasks_add` / `tasks_complete` / `tasks_update` route to the list named by `list_id`,
or — with none — to the default target (the active list, else the first enabled, else local).
Before adding from chat the agent should call **`tasks_lists`** and ask which list when more
than one exists. `tasks_update` edits content (title/notes/due); passing **`to_list_id`**
moves the task (recreate+delete on Google — ADR-0038); `tasks_complete` flips the done flag —
distinct operations. The `Task` domain model is:

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
    list_id: str | None = None                                  # the list (category) — router-stamped
    list_title: str | None = None                               # its human label — router-stamped
    # `completed` is a computed alias: True when status == "done".
```

`list_id` / `list_title` are **not stored** — the router stamps them when it aggregates the
board so each card knows its category and routes its mutations to the owning list (ADR-0036).

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
  (`tasks_complete`, one-tap) and **Edit** (`tasks_update`, a form prefilled from the card),
  both carrying the task's `list_id` so the mutation routes to the **owning** list; the board
  offers **Add task** (`tasks_add`, a form) whose **list picker** chooses the target list
  (a labeled `field_choices` entry, value = list id → label = title). With **two or more**
  writable lists the Edit form also gains a **List** picker bound to `to_list_id` (prefilled
  to the task's current list); choosing another **moves** the task there — a recreate+delete
  on Google (ADR-0038). The board never carries credentials or business logic — it is data
  plus tool references.

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

### `google` provider

- Calls the Google Tasks REST API (`tasks.googleapis.com`).
- OAuth token fetched from `GET /platform/v1/oauth/google/token` — **no client
  secret or refresh token lives in this module** (ADR-0020 / non-negotiable #8).
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

Unique constraint on `(tenant_id, id)`. A read's `scope` selects the rows by the `completed` flag (ADR-0049): `open` (the default, `completed = FALSE`), `done` (`completed = TRUE`), or `all` (no filter); all ordered by `created_at DESC`. `tasks_list` always reads the `open` scope; the board's *Show* filter passes the chosen scope.

Schema is created automatically by `TaskStore.init()` at startup, which also **reconciles
columns added after the table's first release** — there is no migration framework, so `init()`
runs `create_all` and then `ALTER TABLE … ADD COLUMN` for any model column missing from an
existing table (additive only; the v0.5.0 `status`/`priority`/`tags` fields). Without this, a
database provisioned before v0.5.0 has no `status` column and **every** task read (the board,
`tasks_list`, the attachment picker, the resolver) 500s with `column tasks_local.status does
not exist` (#247). Destructive changes — drops, renames, type changes, `NOT NULL` backfills —
still require a real migration.

### Google provider

No local persistence — tasks live in Google Tasks. The OAuth token is stored in
OpenBao by the core's OAuth subsystem under `oauth/tokens/google` (tenant-scoped).

## Dependencies

core-app (OAuth token endpoint) · Postgres (`local` provider only) · NATS.

## Run & extend

```bash
# One container backs both local + Google; the operator picks the active list in the UI:
docker compose up -d tasks
```

To use Google: connect the account and pick a list in **Modules → Tasks → Lists** (or connect
from **Settings**). No restart or env change is needed (ADR-0030).

**Adding a new provider** — implement the `TasksProvider` Protocol (including `is_available`
and `list_collections`) in a new file, add it to the `external` map in `app.py` (keyed by its
account id), and add it to `PROVIDER_LABELS` + `collections.providers` in `service.py`. No
tool or model changes are needed.

Package `epicurus_tasks`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `Task` domain model (provider-neutral). |
| `providers.py` | `TasksProvider` Protocol — the swappable back-end seam (list (by `scope`)/add/complete/update/delete + `get_task` + `is_available`/`list_collections`). |
| `local_provider.py` | `LocalTasksProvider` — Postgres-backed task store (the silent default). |
| `google_provider.py` | `GoogleTasksProvider` — Google Tasks REST API (+ list-discovery + delete). |
| `router.py` | `TasksRouter` — routes ops to the operator's active list across local + Google (ADR-0030); moves a task between lists by recreate+delete (ADR-0038). |
| `db.py` | `TaskStore` — SQLAlchemy ORM + CRUD helpers (list/add/complete/update/get/delete) for the local store. |
| `service.py` | MCP tools (`tasks_list`/`tasks_lists`/`tasks_add`/`tasks_complete`/`tasks_update`) + manifest UI (+ `collections` spec) + the Tasks `board` page (`PageSpec` + the pure `build_tasks_board` builder with group-by/scope **view controls** and `coerce_group`/`coerce_scope`, ADR-0049) + entity-reference, hover-card & chat-attachment helpers + `tasks_accounts` (the `/accounts` view). |
| `app.py` | Lifespan, provider router wiring, `GET /status`, `GET /accounts`, `GET /pages/{id}`, `GET /attachments[/{ref_id}]`, `GET /resolve/{kind}/{ref_id}`, app factory. |
| `settings.py` | `TasksSettings` (adds `platform_url`, `database_url`). |
