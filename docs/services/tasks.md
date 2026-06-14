# tasks — provider-neutral task management

**`epicurus-tasks`** is a sidecar module that manages tasks via a swappable provider
back-end (ADR-0016). The agent interacts with one stable tool surface regardless of
which provider is active. **v0.1 ships two providers:**

- **`local`** (default) — tasks stored in the module's own tenant-scoped Postgres
  table. Works with no external account.
- **`google`** — tasks in Google Tasks, via the Google Tasks REST API. Token is
  fetched from the core's OAuth vault; no credential lives in this module.

Post-v0.1: add Todoist, Microsoft To Do, or any other provider without reshaping the
tool surface. Host port **8087**.

## The contract it exposes

### MCP tools (agent-facing)

| Tool | Inputs | Returns |
| --- | --- | --- |
| `tasks_list(list_id?)` | `list_id`: optional list identifier (omit for default) | List of open `Task` objects for the tenant. |
| `tasks_add(title, notes?, due?, list_id?)` | `title`: required; `notes`/`due`/`list_id`: optional | The created `Task`. |
| `tasks_complete(task_id, list_id?)` | `task_id`: provider task ID; `list_id`: optional | The updated `Task` with `completed=True`. |

All three tools are **provider-agnostic** — `list_id` maps to `@default` (Google)
or is silently ignored (local). The `Task` domain model is:

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
| `GET /mcp` (streamable-HTTP) | MCP tool surface (served by FastMCP). |

### Web UI (manifest, ADR-0007 Tier 1)

| Panel | What it shows / does |
| --- | --- |
| **Status** | Active provider name (polled from `GET /status`). |
| **Actions** | **List tasks** — calls `tasks_list` through the core. |

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
| `providers.py` | `TasksProvider` Protocol — the swappable back-end seam. |
| `local_provider.py` | `LocalTasksProvider` — Postgres-backed task store. |
| `google_provider.py` | `GoogleTasksProvider` — Google Tasks REST API. |
| `db.py` | `TaskStore` — SQLAlchemy ORM + CRUD helpers for the local store. |
| `service.py` | MCP tools (`tasks_list`, `tasks_add`, `tasks_complete`) + manifest UI. |
| `app.py` | Lifespan, provider selection, `GET /status`, app factory. |
| `settings.py` | `TasksSettings` (adds `tasks_provider`, `platform_url`, `database_url`). |
