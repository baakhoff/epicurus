# Reference: `db`

`epicurus_core.db` — schema management for store-owning services: the **Alembic runner** every
migrated service calls at startup, the shared `env.py` body, the idempotent operations a baseline
revision is rendered against, and the **additive reconcile** that preceded all of it.

For the workflow — how to author a revision, what the startup paths mean, the backfill rule, what
each gate proves — see **[Schema migrations](../developer/migrations.md)**. This page is the API.

> Unlike the rest of `epicurus-core`, none of this is importable from the top level. It needs
> SQLAlchemy and (for the migration modules) Alembic — the optional `db` extra — so a module
> without a database carries no ORM dependency. Import the submodules directly. A **migrated**
> service declares `epicurus-core[db]` in its `pyproject.toml`; that extra is what puts Alembic
> in its image.

Four modules, split by what each one drags in:

| Module | Import | Needs Alembic |
| --- | --- | --- |
| `epicurus_core.db` | `from epicurus_core.db import ensure_columns` | no |
| `epicurus_core.db.migrations` | `from epicurus_core.db.migrations import run_migrations` | yes |
| `epicurus_core.db.alembic_env` | `from epicurus_core.db.alembic_env import run` | yes |
| `epicurus_core.db.ops` | `from epicurus_core.db import ops as ep` | yes |

`epicurus_core.db` itself imports no Alembic on purpose: a service still on the pre-Alembic path
installs plain `epicurus-core` plus SQLAlchemy and must not break when the shared library gains a
migration runner.

## `migrations`

### `run_migrations`

```python
async def run_migrations(
    engine: AsyncEngine,
    *,
    service: str,
    script_location: Path,
    metadatas: Sequence[MetaData],
) -> MigrationOutcome
```

Bring *service*'s schema to head. The one call a migrated service makes, from its lifespan,
before anything reads or writes a row:

```python
from epicurus_core.db.migrations import run_migrations
from epicurus_storage.migrations import METADATAS, SCRIPT_LOCATION


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await run_migrations(
        engine, service=MODULE_NAME, script_location=SCRIPT_LOCATION, metadatas=METADATAS
    )
    ...
```

- `service` — the `services/<name>` directory name. The version table and the advisory lock both
  key on it.
- `script_location` — the service's `migrations/` directory (`SCRIPT_LOCATION`).
- `metadatas` — every `MetaData` the service owns, one per store module's `DeclarativeBase`
  (`METADATAS`). A store module left out is a store whose drift nothing detects.

**In the lifespan, not a separate init step**: a container has one entry point on both runtimes
the stack supports, and a Kubernetes-only init container would put the schema behind a code path
Compose never executes.

**Concurrency.** On Postgres it takes a **session-level advisory lock** keyed on the service name,
on a connection of its own, for the whole run — so two replicas (the chart can scale a module past
one pod) or a restart overlapping a start never run `upgrade head` together. The loser waits and
then finds nothing to do. Off Postgres there is no lock; it says so at DEBUG. Alembic runs through
`connection.run_sync(...)`, so the service's asyncpg engine is the only driver needed.

Returns which starting state it found, for the startup log (`database=…`) and for tests:

| `MigrationOutcome` | Meaning |
| --- | --- |
| `"fresh"` | No version table, none of the service's tables — a new deployment. |
| `"adopted"` | The service's tables exist but no version table — a database built by the pre-Alembic path, now coming under management. |
| `"managed"` | A version table exists; an ordinary upgrade, usually a no-op. |

The classification is **informational**. There is one code path — `upgrade head` — in all three
cases, because the baseline revision is itself idempotent (see [`ops`](#ops)).

### `version_table_name`

```python
def version_table_name(service: str) -> str
```

`alembic_version_<service>`, with hyphens folded to underscores (`core-app` →
`alembic_version_core_app`) so the name needs no quoting. Every service shares one Postgres
database and owns disjoint tables in it, so each needs its **own** version table — a shared
`alembic_version` would have seven services overwriting each other's head revision.

### `advisory_lock_key`

```python
def advisory_lock_key(service: str) -> int
```

A stable signed 64-bit key for `pg_advisory_lock`: BLAKE2b over
`epicurus:db:migrations:<service>`, namespaced so it cannot collide with another subsystem's use
of advisory locks in the same database. Deliberately **not** Python's `hash()`, which is salted per
interpreter — two replicas would derive different keys and neither would ever wait.

### `alembic_config`

```python
def alembic_config(
    *,
    script_location: Path,
    metadatas: Sequence[MetaData],
    version_table: str,
    connection: Connection | None = None,
) -> Config
```

Builds the Alembic `Config` programmatically. **There is no `alembic.ini` anywhere in the repo**: a
per-service ini would be seven copies of the same lines, and the configuration that matters (the
connection, the metadata list, the version table) cannot live in a static file — it travels in
`Config.attributes`, which `alembic_env.run` reads. Running the bare `alembic` CLI against this
tree is unsupported; `scripts/migrate.py` is the entry point.

## `alembic_env`

```python
def run() -> None
```

The shared `env.py` body. Each service's own `env.py` is three lines:

```python
from epicurus_core.db.alembic_env import run

run()
```

Everything that matters lives here, in one reviewable place:

- the per-service **`version_table`**;
- **`render_as_batch=True`** — SQLite cannot `ALTER` much, so a revision that alters a column runs
  inside a batch block and one revision sequence stays runnable on both Postgres and SQLite;
- **`compare_type=True`** and **`compare_server_default=True`** — the drift gate's whole job is to
  fail when a model and its revisions disagree, and a silently widened column or a changed default
  is exactly the drift that reaches production as a runtime error;
- an **`include_name` filter** restricted to the tables this service owns. All services share one
  database, so an unfiltered compare would see the other six services' tables as "present but not
  in my models" and propose dropping them — `alembic check` could never pass.

Online only: it requires a live `Connection` in `config.attributes` and raises a pointed
`RuntimeError` without one. There is no offline (`--sql`) arm — an offline render could not consult
the live table shape the idempotent baseline depends on.

## `ops`

The operations a **baseline** revision is rendered against, imported as `ep`:

```python
from epicurus_core.db import ops as ep


def upgrade() -> None:
    ep.create_table(
        "storage_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), server_default=sa.text("'fs'"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    ep.create_index(ep.f("ix_storage_files_tenant"), "storage_files", ["tenant"], unique=False)
```

A baseline describes the schema as of the day a service adopted Alembic — which on every
already-running deployment *already exists*, so a plain `op.create_table` would fail on exactly the
databases that matter most. Stamping the baseline instead would need a separate "is this a
pre-Alembic database?" branch, and on a deployment that skipped the adoption release it would
silently swallow every revision between the stamp and head — including a backfill, which is #903's
fix. So the baseline is made idempotent and there is one code path.

| Function | Table absent | Table present |
| --- | --- | --- |
| `create_table(name, *columns, **kw)` | `op.create_table` | additive reconcile over the declared columns ([`ensure_columns`](#ensure_columns)) |
| `create_index(name, table, columns, unique=False)` | — | creates it, or skips if an index by that name exists; warns and skips if a column it covers could not be added |
| `f(name)` | `op.f` verbatim — "this name is final" | same |

`create_table`'s present-arm deliberately does **not** add a constraint or alter an existing
column: a table that exists but carries no unique constraint is beyond an additive repair, and
inventing a `DROP`/`ALTER` inside a baseline would make it destructive on databases nobody has
inspected. Such a case gets its own, reviewable revision.

**Only the baseline is written this way.** Every revision after it is ordinary Alembic, because
after adoption the database's state is known exactly.

## `ensure_columns`

```python
def ensure_columns(sync_conn: Connection, table: FromClause, columns: Iterable[str]) -> None
```

The **additive reconcile** (#249, ADR-0067), the schema mechanism that preceded migrations. A
service evolved its Postgres schema with `Base.metadata.create_all`, which creates a *missing*
table but never alters an *existing* one — so a column added to a model after that table's first
release silently never reached an already-provisioned database, and every query referencing it
failed on Postgres with `column … does not exist` (#214, #218). This adds those columns in place.

It has two callers now:

- a store that has **not** yet adopted Alembic, from its `init()` — the original use;
- `epicurus_core.db.ops.create_table`, when a baseline meets a table that already exists.

For a store still on the pre-Alembic path:

```python
from epicurus_core.db import ensure_columns

# Columns introduced after the table's first release — the store owns this list.
_ADDED_COLUMNS = ("status", "priority", "tags")


class TaskStore:
    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
            await conn.run_sync(lambda c: ensure_columns(c, _StoredTask.__table__, _ADDED_COLUMNS))
```

- `table` is a mapped class's `__table__` (typed `FromClause` by SQLAlchemy, always a `Table` at
  runtime — passing anything else raises `TypeError`).
- `columns` names the columns to reconcile. For a pre-Alembic store that is the post-release list,
  which the store owns; `ops.create_table` passes the baseline's full column list.
- **Idempotent** — a column already present is skipped, so it is safe to run on every startup.
- The column type is compiled for the live dialect, so the same call is portable across Postgres
  (production) and SQLite (the unit tests).

### What it reproduces

A reconciled column is meant to match what `create_all` would have produced, so a column looks
the same whether the table was freshly created or reconciled:

| Model column | Reconcile adds |
| --- | --- |
| nullable, no server default | `name TYPE` (nullable) |
| `server_default=…` (e.g. `"'fs'"`, `"'{}'"`) | `name TYPE [NOT NULL] DEFAULT …` — the default backfills existing rows |
| **NOT NULL, no server default** | `name TYPE` **nullable** — the one exception (see below) |

### Additive only

It only **adds** columns. It never drops, renames, retypes, or backfills beyond a server default.
A column the model marks `NOT NULL` but gives *no* server default cannot be added to a populated
table — there is nothing to backfill the existing rows with — so it is added **nullable** and the
row-reader coerces the resulting `NULL` to the model's Python-side default (e.g. calendar's
`all_day` → `False`). That `NULL` is #903's cause, and a migration is what finally fixes it: see
the [backfill rule](../developer/migrations.md#the-backfill-rule). Drops, renames, type changes and
true NOT-NULL backfills are ordinary revisions now — for a **migrated** service. For one still on
this path, the interim rule in [Versioning](../developer/versioning.md#schema-changes-before-10)
still holds.
