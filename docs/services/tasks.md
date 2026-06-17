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

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Inputs | Returns |
| --- | --- | --- |
| `tasks_list(list_id?)` | `list_id`: optional list identifier (omit for default) | Open tasks as **entity-reference chips** (ADR-0019), newest first. |
| `tasks_add(title, notes?, due?, list_id?)` | `title`: required; `notes`/`due`/`list_id`: optional | The created `Task`. |
| `tasks_complete(task_id, list_id?)` | `task_id`: provider task ID; `list_id`: optional | The updated `Task` with `completed=True`. |
| `tasks_update(task_id, title?, notes?, due?, list_id?)` | `task_id`: provider task ID; only the fields passed change, the rest are left intact | The updated `Task`. |

All four tools are **provider-agnostic** — `list_id` maps to `@default` (Google)
or is silently ignored (local). `tasks_update` edits content (title/notes/due);
`tasks_complete` flips the done flag — distinct operations. The `Task` domain model is:

```python
class Task(BaseModel):
    id: str
    title: str
    notes: str | None = None
    due: str | None = None      # ISO date, e.g. "2025-01-15"
    completed: bool = False
    completed_at: str | None = None
```

### HTTP

| Endpoint | Description |
| --- | --- |
| `GET /health` | Liveness probe. |
| `GET /metrics` | Prometheus metrics. |
| `GET /manifest` | Module manifest (tools, UI declaration). |
| `GET /status` | Active provider name: `{"provider": "local" \| "google"}`. |
| `GET /pages/{id}` | Page data for a manifest-declared page (`board`); the core proxies it (ADR-0018). 404 for an unknown id. |
| `GET /attachments` | Chat-attachment picker (ADR-0019): open tasks as `{ref_id, kind, title}`. Core-proxied. |
| `GET /attachments/{ref_id}` | Resolve an attached task to `{title, excerpt}` (ADR-0019); missing task is `404`. Core-proxied. |
| `GET /resolve/{kind}/{ref_id}` | Hover-card resolver for a referenced task (ADR-0019); `kind` is `task`. Returns a `HoverCard`; unknown kind / missing task is `404`. Core-proxied. |
| `GET /mcp` (streamable-HTTP) | MCP tool surface (served by FastMCP). |

### Web UI (manifest, ADR-0007 Tier 1)

| Panel | What it shows / does |
| --- | --- |
| **Status** | Active provider name (polled from `GET /status`). |
| **Actions** | None — `tasks_list` returns entity-reference chips (surfaced in chat), so it is not a card-action button. |
| **Tasks page** | A left-nav `board` page (see below). |

### The Tasks page — `board` archetype (ADR-0018)

The module declares one page — `{id: "board", title: "Tasks", archetype: "board"}` — and
serves its data at `GET /pages/board`. The core renders it; the module ships **no markup**.

- **Columns** group the tenant's **open** tasks by due date: **Overdue**, **Today**,
  **Upcoming**, **No date** (empty columns are dropped). Completing a task removes it from
  the board, mirroring the provider's open-tasks semantics. Bucketing is a pure function,
  `build_tasks_board(tasks, today=…)`, so it is unit-tested without a clock — ISO date
  strings compare lexicographically, so no parsing is needed.
- **Mutations are declarative actions** that name an MCP tool; the shell invokes it through
  the core (validated against the manifest) and refetches. Each card offers **Complete**
  (`tasks_complete`, one-tap) and **Edit** (`tasks_update`, a form prefilled from the card);
  the board offers **Add task** (`tasks_add`, a form). The board never carries credentials
  or business logic — it is data plus tool references.

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

`TasksSettings` extends [`CoreSettings`](../reference/config.md):

| Env var | Default | Meaning |
| --- | --- | --- |
| `TASKS_PROVIDER` | `local` | Active provider: `"local"` or `"google"`. |
| `PLATFORM_URL` | `http://core-app:8080` | Core service URL for OAuth token and future platform API calls. |
| `DATABASE_URL` | `postgresql+asyncpg://…/epicurus` | Postgres DSN. Used only by the `local` provider. |

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

Unique constraint on `(tenant_id, id)`. `tasks_list` returns only rows with `completed = FALSE`, ordered by `created_at DESC`.

Schema is created automatically by `TaskStore.init()` at startup.

### Google provider

No local persistence — tasks live in Google Tasks. The OAuth token is stored in
OpenBao by the core's OAuth subsystem under `oauth/tokens/google` (tenant-scoped).

## Dependencies

core-app (OAuth token endpoint) · Postgres (`local` provider only) · NATS.

## Run & extend

```bash
# Local provider (default — no Google account needed):
docker compose up -d tasks

# Google provider (requires a connected Google account):
TASKS_PROVIDER=google docker compose up -d tasks
```

**Adding a new provider** — implement the `TasksProvider` Protocol in a new file,
set it in `app.py`'s provider-selection block, and add the provider name to
`TASKS_PROVIDER`. No tool or model changes are needed.

Package `epicurus_tasks`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `Task` domain model (provider-neutral). |
| `providers.py` | `TasksProvider` Protocol — the swappable back-end seam (list/add/complete/update + `get_task`). |
| `local_provider.py` | `LocalTasksProvider` — Postgres-backed task store. |
| `google_provider.py` | `GoogleTasksProvider` — Google Tasks REST API. |
| `db.py` | `TaskStore` — SQLAlchemy ORM + CRUD helpers (list/add/complete/update/get/delete) for the local store. |
| `service.py` | MCP tools (`tasks_list`/`tasks_add`/`tasks_complete`/`tasks_update`) + manifest UI + the Tasks `board` page (`PageSpec` + the pure `build_tasks_board` builder) + entity-reference, hover-card & chat-attachment helpers (`task_entity_ref`/`task_hover_card`/`tasks_attachments`/`task_attachment`/`fetch_task`). |
| `app.py` | Lifespan, provider selection, `GET /status`, `GET /pages/{id}`, `GET /attachments[/{ref_id}]`, `GET /resolve/{kind}/{ref_id}`, app factory. |
| `settings.py` | `TasksSettings` (adds `tasks_provider`, `platform_url`, `database_url`). |
