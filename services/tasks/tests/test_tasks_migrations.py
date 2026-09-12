"""Tasks' migration environment, exercised end to end on SQLite (#929, ADR-XXXX).

This is the service's second migration-managed store, so it is also where the
"one `run_migrations` call, several `DeclarativeBase` objects" shape gets its first real
exercise: `epicurus_tasks.migrations.METADATAS` carries three bases (`db.py`,
`lead_time_prefs.py`, `scheduler.py`) across four tables, and one `upgrade head` has to bring
all of them to the models' shape together.

Three things are worth proving here, mirroring `services/storage/tests/test_storage_migrations.py`
(the reference):

* the revisions and the models agree — `upgrade head` from empty produces exactly what the
  three stores' `create_all` would, table for table and column for column;
* a database built **before** tasks adopted Alembic is adopted correctly — including the
  pre-#218 `tasks_local` shape that predates `status`/`priority`/`tags`/`repeat`, which used to
  be `TaskStore._ensure_columns`' job (#247) and is now the baseline's;
* running it twice is a no-op.

Everything runs against **file-backed** SQLite under `tmp_path`: the migration runner opens its
own connections, and the in-memory/`StaticPool` fixture shares one DBAPI connection across every
session, which is the setup that silently swallows a concurrent writer's commit (see AGENTS.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_core.db.migrations import (
    advisory_lock_key,
    run_migrations,
    version_table_name,
)
from epicurus_tasks.db import RepeatStore, TaskStore, _Base
from epicurus_tasks.lead_time_prefs import LeadTimePrefsStore, _LeadTimeBase
from epicurus_tasks.migrations import METADATAS, SCRIPT_LOCATION, SERVICE
from epicurus_tasks.scheduler import FiredMarkerStore, _MarkerBase

HEAD = "0001"

# The exact pre-#218 tasks_local shape (mirrors the live drifted table this repo has run):
# the rich fields (status/priority/tags) and the recurrence field (repeat, #471) are absent.
# Also missing the indexes on ``id``/``tenant_id`` the model has always carried, so the
# baseline's index-repair arm gets exercised too.
_LEGACY_TASKS_LOCAL = (
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


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A file-backed SQLite engine with default pooling, disposed on teardown."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tasks.sqlite'}")
    try:
        yield engine
    finally:
        await engine.dispose()


async def _migrate(engine: AsyncEngine) -> str:
    return await run_migrations(
        engine,
        service=SERVICE,
        script_location=SCRIPT_LOCATION,
        metadatas=METADATAS,
    )


async def _shape(engine: AsyncEngine) -> dict[str, dict[str, str]]:
    """Every table and column SQLite reports, as ``{table: {column: type}}``."""

    def read(sync_conn: sa.Connection) -> dict[str, dict[str, str]]:
        inspector = sa.inspect(sync_conn)
        return {
            table: {column["name"]: str(column["type"]) for column in inspector.get_columns(table)}
            for table in inspector.get_table_names()
            if not table.startswith("alembic_version")
        }

    async with engine.connect() as conn:
        return await conn.run_sync(read)


async def _indexes(engine: AsyncEngine, table: str) -> set[str]:
    def read(sync_conn: sa.Connection) -> set[str]:
        return {ix["name"] or "" for ix in sa.inspect(sync_conn).get_indexes(table)}

    async with engine.connect() as conn:
        return await conn.run_sync(read)


async def _version(engine: AsyncEngine) -> str | None:
    """The revision the version table records, or ``None`` if there is no version table."""

    def read(sync_conn: sa.Connection) -> str | None:
        table = version_table_name(SERVICE)
        if not sa.inspect(sync_conn).has_table(table):
            return None
        row = sync_conn.exec_driver_sql(f"SELECT version_num FROM {table}").scalar()
        return None if row is None else str(row)

    async with engine.connect() as conn:
        return await conn.run_sync(read)


# ── The revisions and the models agree ────────────────────────────────────────


async def test_upgrade_head_from_empty_matches_the_models(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """``upgrade head`` and the three stores' ``create_all`` produce the same schema.

    The unit tests build schema with ``create_all`` because an Alembic run per test is needless
    cost; production builds it from the revisions. This is what makes those two the same thing,
    on top of the `migrations` CI gate that re-proves it on Postgres.
    """
    outcome = await _migrate(engine)
    assert outcome == "fresh"
    assert await _version(engine) == HEAD
    migrated = await _shape(engine)

    direct = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'direct.sqlite'}")
    try:
        await TaskStore(direct).init()
        await LeadTimePrefsStore(direct).init()
        await FiredMarkerStore(direct).init()
        assert migrated == await _shape(direct)
    finally:
        await direct.dispose()
    assert {"tasks_local", "task_repeats", "tasks_lead_time_prefs", "tasks_fired_markers"} <= set(
        migrated
    )


async def test_the_stores_work_against_a_migrated_schema(engine: AsyncEngine) -> None:
    """A round-trip through every store on a schema nothing but the revisions built."""
    await _migrate(engine)

    task = await TaskStore(engine).add_task(
        tenant_id="test", title="write the ADR", notes=None, due=None
    )
    assert (await TaskStore(engine).get_task(tenant_id="test", task_id=task.id)) is not None

    await RepeatStore(engine).set(
        tenant_id="test", list_id="@default", task_id="g1", rrule="FREQ=DAILY"
    )
    assert (
        await RepeatStore(engine).get(tenant_id="test", list_id="@default", task_id="g1")
        == "FREQ=DAILY"
    )

    await LeadTimePrefsStore(engine).set_lead_days("test", 3)
    assert await LeadTimePrefsStore(engine).get_lead_days("test") == 3

    assert await FiredMarkerStore(engine).try_claim(tenant="test", task_id="t1", marker="overdue")
    assert await FiredMarkerStore(engine).has_fired(tenant="test", task_id="t1", marker="overdue")


# ── Adopting a database built before Alembic ──────────────────────────────────


async def test_a_pre_alembic_database_is_adopted_and_keeps_its_rows(engine: AsyncEngine) -> None:
    """``create_all``-built tables, no version table: the baseline reconciles rather than fails.

    This is every existing tasks deployment on the release that adopts migrations. The tables
    are already there, so a plain ``op.create_table`` would abort the upgrade; the baseline's
    idempotent ops take the reconcile arm instead, the rows survive, and the version table lands
    at head.
    """
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
        await conn.run_sync(_LeadTimeBase.metadata.create_all)
        await conn.run_sync(_MarkerBase.metadata.create_all)
    await TaskStore(engine).add_task(tenant_id="test", title="pre-existing", notes=None, due=None)
    await LeadTimePrefsStore(engine).set_lead_days("test", 2)
    await FiredMarkerStore(engine).try_claim(tenant="test", task_id="t1", marker="due_soon")
    assert await _version(engine) is None

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD

    tasks = await TaskStore(engine).list_tasks(tenant_id="test", scope="all")
    assert len(tasks) == 1 and tasks[0].title == "pre-existing"
    assert await LeadTimePrefsStore(engine).get_lead_days("test") == 2
    assert await FiredMarkerStore(engine).has_fired(tenant="test", task_id="t1", marker="due_soon")


async def test_the_baseline_repairs_the_pre_218_rich_field_columns(engine: AsyncEngine) -> None:
    """A deployment that predates status/priority/tags/repeat (#218, #471) gains them.

    The additive reconcile used to do this from ``TaskStore.init()`` via ``_ensure_columns``
    (#247). It is the baseline revision's job now, and the behaviour that matters — a legacy row
    reads back through ``list_tasks``/``get_task`` instead of 500ing on
    ``column tasks_local.status does not exist`` — is unchanged. The other three tables are
    absent entirely here, so the baseline creates them outright alongside the reconcile.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_LEGACY_TASKS_LOCAL)
        await conn.exec_driver_sql(
            "INSERT INTO tasks_local (id, tenant_id, title, completed) "
            "VALUES ('legacy-1', 'local', 'Legacy task', 0)"
        )

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD
    columns = (await _shape(engine))["tasks_local"]
    assert {"status", "priority", "tags", "repeat"} <= set(columns)
    # The indexes the legacy table never had are repaired too.
    assert {"ix_tasks_local_id", "ix_tasks_local_tenant_id"} <= await _indexes(
        engine, "tasks_local"
    )

    store = TaskStore(engine)
    legacy = await store.get_task(tenant_id="local", task_id="legacy-1")
    assert legacy is not None
    assert legacy.status == "open"  # derived from completed=0; status column reads NULL
    assert legacy.priority is None
    assert legacy.tags == []

    # And the repaired columns are writable going forward.
    updated = await store.update_task(
        tenant_id="local", task_id="legacy-1", status="in_progress", priority="high"
    )
    assert updated.status == "in_progress"
    assert updated.priority == "high"


# ── Restart, and the lock that makes a second replica safe ────────────────────


async def test_a_second_run_is_a_no_op(engine: AsyncEngine) -> None:
    """An ordinary restart: already at head, classified ``managed``, nothing changes."""
    await _migrate(engine)
    before = await _shape(engine)
    assert await _migrate(engine) == "managed"
    assert await _shape(engine) == before
    assert await _version(engine) == HEAD


async def test_running_it_twice_over_is_still_the_same_schema(engine: AsyncEngine) -> None:
    """Three runs in a row — the nearest SQLite can get to two replicas starting together.

    Postgres serialises concurrent runners with a session advisory lock; SQLite has no such
    lock, so the runner skips it (there is also no concurrency to serialise — the unit tests
    own their file). What must hold either way is that a second entry into ``upgrade head``
    finds nothing to do rather than failing or duplicating work. The lock itself is exercised
    for real by the `migrations` CI gate.
    """
    assert await _migrate(engine) == "fresh"
    first = await _shape(engine)
    assert await _migrate(engine) == "managed"
    assert await _migrate(engine) == "managed"
    assert await _shape(engine) == first


def test_the_advisory_lock_key_is_stable_for_this_service() -> None:
    """Two replicas must derive the same key, so it cannot come from a salted hash."""
    assert advisory_lock_key(SERVICE) == advisory_lock_key("tasks")
    assert advisory_lock_key(SERVICE) != advisory_lock_key("storage")
    assert version_table_name(SERVICE) == "alembic_version_tasks"
