# epicurus-tasks

Provider-neutral task management module for the epicurus platform (ADR-0016).
Exposes four MCP tools (`tasks_list`, `tasks_add`, `tasks_complete`, `tasks_update`)
backed by a swappable provider — **local Postgres store** (default) or **Google Tasks**.

## Quick start

```bash
# Local provider (no external account needed):
docker compose up -d tasks

# Google Tasks provider (requires a connected Google account):
TASKS_PROVIDER=google docker compose up -d tasks
```

Full documentation: [docs/services/tasks.md](../../docs/services/tasks.md).

## Wire-in checklist (when adding a new module)

1. `services/tasks/compose.yaml` — this file; service + port `8091`.
2. Root `compose.yaml` — `include: services/tasks/compose.yaml` ✓
3. `services/core-app/src/epicurus_core_app/settings.py` — `http://tasks:8080`
   added to `module_urls` ✓

## Tools

| Tool | Description |
| --- | --- |
| `tasks_list(list_id?)` | Return open tasks as entity-reference chips (hover → hover-card, click → detail panel). |
| `tasks_add(title, notes?, due?, list_id?)` | Create a new task. |
| `tasks_complete(task_id, list_id?)` | Mark a task complete. |
| `tasks_update(task_id, title?, notes?, due?, list_id?)` | Edit a task's title, notes, or due date. |

## Adding a provider

1. Create a new class in `src/epicurus_tasks/` that implements the
   `TasksProvider` Protocol (see `providers.py`).
2. Add a branch in `app.py`'s provider-selection block.
3. Document it in `docs/services/tasks.md`.

No changes to tools or the domain model are needed — that is the point of the
provider seam (ADR-0016).
