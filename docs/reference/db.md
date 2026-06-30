# Reference: `db`

`epicurus_core.db` — an **additive schema reconcile** for stores that have no migration
framework. Services evolve their Postgres schema with `Base.metadata.create_all`, which
creates a *missing* table but never alters an *existing* one — so a column added to a model
after that table's first release silently never reaches an already-provisioned database, and
every query that references it fails on Postgres with `column … does not exist`. This helper
adds those columns in place at startup (#249, ADR-0066).

> Unlike the rest of `epicurus-core`, this is **not** importable from the top level. It needs
> SQLAlchemy — the optional `db` extra — so a module without a database carries no ORM
> dependency. Import the submodule directly: `from epicurus_core.db import ensure_columns`.
> (Every store-owning service already depends on SQLAlchemy, so the import resolves.)

## `ensure_columns`

```python
def ensure_columns(sync_conn: Connection, table: FromClause, columns: Iterable[str]) -> None
```

Add any of `columns` that the model defines but the live `table` still lacks. Call it from a
store's `init()`, inside `conn.run_sync`, right after `create_all`:

```python
from epicurus_core.db import ensure_columns

# Columns introduced after the table's first release — the store owns this list.
_ADDED_COLUMNS = ("status", "priority", "tags")

class TaskStore:
    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
            await conn.run_sync(
                lambda c: ensure_columns(c, _StoredTask.__table__, _ADDED_COLUMNS)
            )
```

- `table` is a mapped class's `__table__` (typed `FromClause` by SQLAlchemy, always a `Table`
  at runtime — passing anything else raises `TypeError`).
- `columns` names only the columns added *after* first release; the store owns that list.
- **Idempotent** — a column already present is skipped, so it is safe to run on every startup.
- The column type is compiled for the live dialect, so the same call is portable across
  Postgres (production) and SQLite (the unit tests).

### What it reproduces

A reconciled column is meant to match what `create_all` would have produced, so a column looks
the same whether the table was freshly created or reconciled:

| Model column | Reconcile adds |
| --- | --- |
| nullable, no server default | `name TYPE` (nullable) |
| `server_default=…` (e.g. `"'fs'"`, `"'{}'"`) | `name TYPE [NOT NULL] DEFAULT …` — the default backfills existing rows |
| **NOT NULL, no server default** | `name TYPE` **nullable** — the one exception (see below) |

### Additive only

It only **adds** columns. It never drops, renames, retypes, or backfills beyond a server
default. A column the model marks `NOT NULL` but gives *no* server default cannot be added to a
populated table — there is nothing to backfill the existing rows with — so it is added
**nullable** and the row-reader coerces the resulting `NULL` to the model's Python-side default
(e.g. calendar's `all_day` → `False`). Drops, renames, type changes, and true NOT-NULL
backfills need a real migration (Alembic), which the project has deliberately not adopted yet.
