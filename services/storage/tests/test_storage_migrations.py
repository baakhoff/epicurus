"""Storage's migration environment, exercised end to end on SQLite.

Storage is the first service whose schema is Alembic-managed (#834, ADR-XXXX), so these tests
are also the reference the other service lanes copy. Three things are worth proving here and
only here:

* the revisions and the models agree — ``upgrade head`` from empty produces exactly what
  ``create_all`` would, table for table and column for column, so the test suite's cheaper
  ``init()`` path is not quietly building a different schema from production's;
* a database built **before** storage adopted Alembic is adopted correctly — the baseline
  reconciles its existing table, repairs the column the additive reconcile was responsible
  for, keeps its rows, and leaves the version table at head;
* running it twice is a no-op, and doing so off Postgres neither takes nor breaks on the
  advisory lock that serialises two replicas.

Everything runs against **file-backed** SQLite under ``tmp_path``: the migration runner opens
its own connections, and the in-memory/``StaticPool`` fixture shares one DBAPI connection
across every session, which is the setup that silently swallows a concurrent writer's commit
(see AGENTS.md).
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
from epicurus_storage.db import FileIndex, _Base
from epicurus_storage.migrations import METADATAS, SCRIPT_LOCATION, SERVICE

HEAD = "0001"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A file-backed SQLite engine with default pooling, disposed on teardown."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'storage.sqlite'}")
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
    """``upgrade head`` and ``create_all`` produce the same tables and columns.

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
        await FileIndex(direct).init()
        assert migrated == await _shape(direct)
    finally:
        await direct.dispose()
    assert "storage_files" in migrated


async def test_the_index_works_against_a_migrated_schema(engine: AsyncEngine) -> None:
    """A round-trip through the store on a schema nothing but the revisions built."""
    await _migrate(engine)
    index = FileIndex(engine)
    await index.upsert_batch(
        tenant="test",
        entries=[{"path": "a.txt", "name": "a.txt", "size": 3, "mtime": 1.0, "kind": "file"}],
    )
    entry = await index.get(tenant="test", path="a.txt")
    assert entry is not None
    assert entry.size == 3
    # The unique constraint and the tenant index are part of the baseline, not an afterthought.
    assert await index.count(tenant="test") == {"files": 1, "dirs": 0}


# ── Adopting a database built before Alembic ──────────────────────────────────


async def test_a_pre_alembic_database_is_adopted_and_keeps_its_rows(engine: AsyncEngine) -> None:
    """``create_all``-built tables, no version table: the baseline reconciles rather than fails.

    This is every existing deployment on the release that adopts migrations. The table is
    already there, so a plain ``op.create_table`` would abort the upgrade; the baseline's
    idempotent ops take the reconcile arm instead, the rows survive, and the version table
    lands at head.
    """
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    await FileIndex(engine).upsert_batch(
        tenant="test",
        entries=[{"path": "old.txt", "name": "old.txt", "size": 1, "mtime": 0.0, "kind": "file"}],
    )
    assert await _version(engine) is None

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD
    entry = await FileIndex(engine).get(tenant="test", path="old.txt")
    assert entry is not None, "adopting a database must not lose its rows"


async def test_the_baseline_repairs_a_column_the_old_reconcile_was_responsible_for(
    engine: AsyncEngine,
) -> None:
    """A deployment that predates ``storage_files.source`` gains it, backfilled to ``'fs'``.

    The additive reconcile used to do this from ``init()`` (#249, ADR-0067). It is the baseline
    revision's job now, and the behaviour that matters — an existing row reads back as ``fs``
    rather than ``NULL``, because the column carries a literal ``server_default`` — is
    unchanged. The same case under the old reconcile lived in test_storage_ingest.py.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE storage_files ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63), path VARCHAR(4096), "
            "name VARCHAR(255), size BIGINT, mtime FLOAT, kind VARCHAR(8), updated_at DATETIME)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO storage_files (tenant, path, name, size, mtime, kind, updated_at) "
            "VALUES ('test', 'docs/readme.txt', 'readme.txt', 10, 0, 'file', '2026-01-01 00:00:00')"
        )

    assert await _migrate(engine) == "adopted"
    assert "source" in (await _shape(engine))["storage_files"]
    entry = await FileIndex(engine).get(tenant="test", path="docs/readme.txt")
    assert entry is not None
    assert entry.source == "fs"


async def test_the_baseline_adds_the_index_a_reconciled_table_never_got(
    engine: AsyncEngine,
) -> None:
    """An index belongs to the table's shape too, and the old reconcile never added one."""

    def indexes(sync_conn: sa.Connection) -> set[str]:
        return {ix["name"] or "" for ix in sa.inspect(sync_conn).get_indexes("storage_files")}

    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE storage_files ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63), path VARCHAR(4096), "
            "name VARCHAR(255), size BIGINT, mtime FLOAT, kind VARCHAR(8), "
            "updated_at DATETIME, source VARCHAR(16) DEFAULT 'fs')"
        )
    async with engine.connect() as conn:
        assert await conn.run_sync(indexes) == set()

    await _migrate(engine)
    async with engine.connect() as conn:
        assert "ix_storage_files_tenant" in await conn.run_sync(indexes)


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
    finds nothing to do rather than failing or duplicating work, which is what the Postgres
    loser of the lock sees once it is let through. The lock itself is exercised for real by the
    `migrations` CI gate.
    """
    assert await _migrate(engine) == "fresh"
    first = await _shape(engine)
    assert await _migrate(engine) == "managed"
    assert await _migrate(engine) == "managed"
    assert await _shape(engine) == first


def test_the_advisory_lock_key_is_stable_for_this_service() -> None:
    """Two replicas must derive the same key, so it cannot come from a salted hash."""
    assert advisory_lock_key(SERVICE) == advisory_lock_key("storage")
    assert advisory_lock_key(SERVICE) != advisory_lock_key("calendar")
    assert version_table_name(SERVICE) == "alembic_version_storage"
