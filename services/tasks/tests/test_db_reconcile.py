"""Schema-reconciliation tests for TaskStore.init() (#247).

The store has no migration framework — it relies on ``create_all``, which builds a
missing table but never alters an existing one. A ``tasks_local`` table provisioned
before #218 added ``status`` / ``priority`` / ``tags`` (v0.5.0) is missing those
columns, so on Postgres every task read 500s with
``column tasks_local.status does not exist`` (the board, ``tasks_list``, the attachment
picker, and the resolver all SELECT them). ``TaskStore._ensure_columns`` adds them in
place at startup. These tests reproduce the pre-#218 table on SQLite (which, like
Postgres, errors on a SELECT of a non-existent column) and assert the heal.
"""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_tasks.db import TaskStore

TENANT = "local"

# The exact pre-#218 schema (mirrors the live drifted table): the rich fields are absent.
_LEGACY_SCHEMA = (
    "CREATE TABLE tasks_local ("
    "pk INTEGER PRIMARY KEY, "
    "id VARCHAR(255), "
    "tenant_id VARCHAR(63), "
    "title VARCHAR(1024), "
    "notes TEXT, "
    "due VARCHAR(64), "
    "completed BOOLEAN, "
    "completed_at VARCHAR(64), "
    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
)


def _shared_memory_engine() -> AsyncEngine:
    """An in-memory SQLite engine whose single connection is shared across sessions.

    StaticPool keeps one underlying connection, so the table built here is the same one
    ``TaskStore`` later reads — the only way an ``:memory:`` DB survives across sessions.
    """
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _columns(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("tasks_local")}
        )


async def _make_legacy_table(engine: AsyncEngine) -> None:
    """Build a pre-#218 ``tasks_local`` and seed one open legacy row."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_LEGACY_SCHEMA)
        await conn.exec_driver_sql(
            "INSERT INTO tasks_local (id, tenant_id, title, completed) "
            "VALUES ('legacy-1', 'local', 'Legacy task', 0)"
        )


async def test_init_adds_missing_rich_field_columns() -> None:
    """init() reconciles a pre-#218 table by adding status/priority/tags in place."""
    engine = _shared_memory_engine()
    await _make_legacy_table(engine)
    assert await _columns(engine) == {
        "pk",
        "id",
        "tenant_id",
        "title",
        "notes",
        "due",
        "completed",
        "completed_at",
        "created_at",
    }

    await TaskStore(engine).init()  # must ADD COLUMN rather than leave the table drifted

    assert {"status", "priority", "tags"} <= await _columns(engine)


async def test_legacy_task_reads_back_after_migration() -> None:
    """The reads that previously 500'd (list/get) now succeed for the legacy row.

    A row written before the status column existed has SQL NULL there; ``_row_to_task``
    derives the status from the legacy ``completed`` flag, so it reads back as open.
    """
    engine = _shared_memory_engine()
    await _make_legacy_table(engine)
    store = TaskStore(engine)
    await store.init()

    tasks = await store.list_tasks(tenant_id=TENANT)
    assert len(tasks) == 1
    legacy = tasks[0]
    assert legacy.id == "legacy-1"
    assert legacy.title == "Legacy task"
    assert legacy.status == "open"  # derived from completed=0 (status column is NULL)
    assert legacy.priority is None
    assert legacy.tags == []

    fetched = await store.get_task(tenant_id=TENANT, task_id="legacy-1")
    assert fetched is not None
    assert fetched.id == "legacy-1"


async def test_migrated_columns_are_writable() -> None:
    """After the heal, the new columns are usable — a task with rich fields round-trips."""
    engine = _shared_memory_engine()
    await _make_legacy_table(engine)
    store = TaskStore(engine)
    await store.init()

    created = await store.add_task(
        tenant_id=TENANT,
        title="Rich task",
        notes=None,
        due="2030-01-01",
        status="in_progress",
        priority="high",
        tags=["work", "urgent"],
    )
    reread = await store.get_task(tenant_id=TENANT, task_id=created.id)
    assert reread is not None
    assert reread.status == "in_progress"
    assert reread.priority == "high"
    assert reread.tags == ["work", "urgent"]


async def test_ensure_columns_is_idempotent() -> None:
    """init() runs cleanly twice; the second pass finds every column present and no-ops."""
    engine = _shared_memory_engine()
    await _make_legacy_table(engine)
    store = TaskStore(engine)

    await store.init()
    await store.init()  # second call must not raise on already-present columns

    assert {"status", "priority", "tags"} <= await _columns(engine)
    assert len(await store.list_tasks(tenant_id=TENANT)) == 1


async def test_fresh_db_needs_no_reconcile() -> None:
    """On a fresh DB create_all builds every column, so reconcile is a clean no-op."""
    engine = _shared_memory_engine()
    store = TaskStore(engine)  # no legacy table — create_all owns the schema
    await store.init()

    assert {"status", "priority", "tags"} <= await _columns(engine)
    task = await store.add_task(tenant_id=TENANT, title="Fresh", notes=None, due=None)
    assert task.status == "open"
