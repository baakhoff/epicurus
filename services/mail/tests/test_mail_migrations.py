"""Mail's migration environment, exercised end to end on SQLite (#834, #932).

Mail follows the recipe `storage` set as the reference service (#926). Two things worth
proving here, and only here:

* ``upgrade head`` from empty produces exactly what ``create_all`` would, table for table and
  column for column, so the test suite's cheaper ``init()`` path is not quietly building a
  different schema from production's;
* a database built **before** mail adopted Alembic is adopted correctly — the baseline reaches
  head and keeps its rows — and running it twice is a no-op.

Mail has no post-release ``ensure_columns`` history (every table's columns have existed since
its first release — the reconciled-column lists in the pre-migration `db.py` were all empty
tuples) and no plain-string `server_default` (the only server defaults are `func.now()`), so
unlike `storage` there is no reconciled-column repair case and no `0002` normalisation revision
to cover here — see the PR body's backfill audit for the full reasoning.

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
from epicurus_mail.db import MailCache, _Base
from epicurus_mail.migrations import METADATAS, SCRIPT_LOCATION, SERVICE
from epicurus_mail.provider import MailCursor, MailLabel

HEAD = "0001"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A file-backed SQLite engine with default pooling, disposed on teardown."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail.sqlite'}")
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
        await MailCache(direct).init()
        assert migrated == await _shape(direct)
    finally:
        await direct.dispose()
    assert {
        "mail_thread",
        "mail_label",
        "mail_sync",
        "mail_landing",
        "mail_category",
    } <= set(migrated)


async def test_the_cache_works_against_a_migrated_schema(engine: AsyncEngine) -> None:
    """A round-trip through the store on a schema nothing but the revisions built."""
    await _migrate(engine)
    cache = MailCache(engine)
    await cache.replace_labels(
        tenant_id="test",
        labels=[MailLabel(id="INBOX", title="Inbox", kind="system", unread=3)],
    )
    labels = await cache.get_labels(tenant_id="test")
    assert len(labels) == 1
    assert labels[0].id == "INBOX"
    # The unique constraint is part of the baseline, not an afterthought.
    assert await cache.has_landing(tenant_id="test", label="INBOX") is False


# ── Adopting a database built before Alembic ──────────────────────────────────


async def test_a_pre_alembic_database_is_adopted_and_keeps_its_rows(engine: AsyncEngine) -> None:
    """``create_all``-built tables, no version table: the baseline reconciles rather than fails.

    This is every existing deployment on the release that adopts migrations. The tables are
    already there, so a plain ``op.create_table`` would abort the upgrade; the baseline's
    idempotent ops take the reconcile arm instead, the rows survive, and the version table
    lands at head.
    """
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    cache = MailCache(engine)
    await cache.set_cursor(tenant_id="test", cursor=MailCursor(history_id=123))
    assert await _version(engine) is None

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD
    cursor = await MailCache(engine).get_cursor(tenant_id="test")
    assert cursor.history_id == 123, "adopting a database must not lose its rows"


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
    assert advisory_lock_key(SERVICE) == advisory_lock_key("mail")
    assert advisory_lock_key(SERVICE) != advisory_lock_key("calendar")
    assert version_table_name(SERVICE) == "alembic_version_mail"
