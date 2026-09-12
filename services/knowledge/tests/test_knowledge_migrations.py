"""Knowledge's migration environment, exercised end to end on SQLite.

Follows the pattern `services/storage/tests/test_storage_migrations.py` set as the reference
for phase B (#834, #926, #931): the three things worth proving are that the revisions and the
models agree, that a database built before knowledge adopted Alembic is adopted correctly, and
that running the upgrade twice is a no-op that neither takes nor breaks on the advisory lock.

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
from epicurus_knowledge.db import DocIndex, NoteIndex, VersionStore
from epicurus_knowledge.migrations import METADATAS, SCRIPT_LOCATION, SERVICE
from epicurus_knowledge.module_docs import ModuleDocLedger
from epicurus_knowledge.suggestions import SuggestionAuditStore, SuggestionStore

HEAD = "0003"

TENANT = "test"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A file-backed SQLite engine with default pooling, disposed on teardown."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'knowledge.sqlite'}")
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


async def _init_every_store(engine: AsyncEngine) -> None:
    """Build every store's tables straight from the models — the direct-``init()`` path."""
    await NoteIndex(engine).init()
    await DocIndex(engine).init()
    await VersionStore(engine).init()
    await ModuleDocLedger(engine).init()
    await SuggestionStore(engine).init()
    await SuggestionAuditStore(engine).init()


def _shape_sync(sync_conn: sa.Connection) -> dict[str, dict[str, str]]:
    inspector = sa.inspect(sync_conn)
    return {
        table: {column["name"]: str(column["type"]) for column in inspector.get_columns(table)}
        for table in inspector.get_table_names()
        if not table.startswith("alembic_version")
    }


async def _shape(engine: AsyncEngine) -> dict[str, dict[str, str]]:
    """Every table and column SQLite reports, as ``{table: {column: type}}``."""
    async with engine.connect() as conn:
        return await conn.run_sync(_shape_sync)


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
    """``upgrade head`` and every store's ``create_all`` produce the same tables and columns."""
    outcome = await _migrate(engine)
    assert outcome == "fresh"
    assert await _version(engine) == HEAD
    migrated = await _shape(engine)

    direct = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'direct.sqlite'}")
    try:
        await _init_every_store(direct)
        assert migrated == await _shape(direct)
    finally:
        await direct.dispose()
    assert set(migrated) == {
        "knowledge_notes",
        "knowledge_doc_index",
        "knowledge_versions",
        "knowledge_module_docs",
        "knowledge_suggestions",
        "knowledge_suggestion_decisions",
    }


async def test_the_suggestion_store_works_against_a_migrated_schema(engine: AsyncEngine) -> None:
    """A round-trip through a store on a schema nothing but the revisions built."""
    await _migrate(engine)
    store = SuggestionStore(engine)
    added = await store.add(
        tenant=TENANT,
        path="a.md",
        operation="create",
        proposed_content="hello",
        origin="agent",
        note="",
    )
    fetched = await store.get(tenant=TENANT, sid=added.sid)
    assert fetched is not None
    assert fetched.to_path == ""  # the server default, not a stray literal quote pair


# ── Adopting a database built before Alembic ──────────────────────────────────


async def test_a_pre_alembic_database_is_adopted_and_keeps_its_rows(engine: AsyncEngine) -> None:
    """``create_all``-built tables, no version table: the baseline reconciles rather than fails.

    This is every existing deployment on the release that adopts migrations. The tables are
    already there, so a plain ``op.create_table`` would abort the upgrade; the baseline's
    idempotent ops take the reconcile arm instead, the rows survive, and the version table
    lands at head.
    """
    await _init_every_store(engine)
    await NoteIndex(engine).upsert(
        tenant=TENANT, note_path="a.md", mtime_ns=1, content_hash="x", chunk_count=1
    )
    assert await _version(engine) is None

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD
    record = await NoteIndex(engine).get(tenant=TENANT, note_path="a.md")
    assert record is not None, "adopting a database must not lose its rows"


async def test_the_baseline_repairs_a_column_the_old_reconcile_was_responsible_for(
    engine: AsyncEngine,
) -> None:
    """A deployment that predates ``knowledge_suggestions.to_path`` gains it, backfilled.

    The additive reconcile used to do this from ``SuggestionStore.init()`` (#220, ADR-0067). It
    is the baseline revision's job now, and the behaviour that matters — an existing row reads
    back with ``to_path == ""`` rather than ``NULL`` — is unchanged.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE knowledge_suggestions ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63), sid VARCHAR(32), path VARCHAR(4096), "
            "operation VARCHAR(16), proposed_content TEXT, origin VARCHAR(64), note TEXT, "
            "created_at DATETIME)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO knowledge_suggestions "
            "(tenant, sid, path, operation, proposed_content, origin, note, created_at) "
            "VALUES ('test', 'sid1', 'a.md', 'create', 'hi', 'agent', '', "
            "'2026-01-01 00:00:00')"
        )

    assert await _migrate(engine) == "adopted"
    assert "to_path" in (await _shape(engine))["knowledge_suggestions"]
    row = await SuggestionStore(engine).get(tenant="test", sid="sid1")
    assert row is not None
    assert row.to_path == ""


async def test_the_baseline_adds_an_index_a_reconciled_table_never_got(
    engine: AsyncEngine,
) -> None:
    """An index belongs to the table's shape too, and the old reconcile never added one."""

    def indexes(sync_conn: sa.Connection) -> set[str]:
        return {ix["name"] or "" for ix in sa.inspect(sync_conn).get_indexes("knowledge_notes")}

    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE knowledge_notes ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63), note_path VARCHAR(4096), "
            "mtime_ns BIGINT, content_hash VARCHAR(64), chunk_count INTEGER, "
            "indexed_at DATETIME, "
            "CONSTRAINT uq_knowledge_tenant_note UNIQUE (tenant, note_path))"
        )
    async with engine.connect() as conn:
        assert await conn.run_sync(indexes) == set()

    await _migrate(engine)
    async with engine.connect() as conn:
        assert "ix_knowledge_notes_tenant" in await conn.run_sync(indexes)


async def test_revision_0002_normalises_the_doubled_quote_to_path_default(
    engine: AsyncEngine,
) -> None:
    """The first change the additive reconcile could never have made — it *alters* a column.

    ``to_path`` was declared ``server_default="''"``, a *plain string*, which SQLAlchemy quotes
    as a literal: ``create_all`` therefore wrote ``DEFAULT ''''''`` while the reconcile, pasting
    the same string in as raw SQL, wrote ``DEFAULT ''``. Two databases, two different defaults,
    and an insert that omitted the column got a value with two literal quote characters in it.
    Nothing noticed because every insert sets ``to_path`` explicitly and the read side treats a
    falsy value as "no destination" either way.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE knowledge_suggestions ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63) NOT NULL, sid VARCHAR(32) NOT NULL, "
            "path VARCHAR(4096) NOT NULL, operation VARCHAR(16) NOT NULL, "
            "proposed_content TEXT NOT NULL, origin VARCHAR(64) NOT NULL, note TEXT NOT NULL, "
            "to_path VARCHAR(4096) DEFAULT '''''' NOT NULL, "
            "created_at DATETIME NOT NULL)"
        )
        # A row the old default produced: its stored `to_path` carries the two quote chars.
        await conn.exec_driver_sql(
            "INSERT INTO knowledge_suggestions "
            "(tenant, sid, path, operation, proposed_content, origin, note, created_at) "
            "VALUES ('test', 'sid1', 'old.md', 'create', 'hi', 'agent', '', "
            "'2026-01-01 00:00:00')"
        )
    async with engine.connect() as conn:
        stored = (await conn.execute(sa.text("SELECT to_path FROM knowledge_suggestions"))).scalar()
    assert stored == "''", "this test is pointless unless the old default really did this"

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD

    async with engine.connect() as conn:
        repaired = (
            await conn.execute(sa.text("SELECT to_path FROM knowledge_suggestions"))
        ).scalar()
        ddl = (
            await conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE name = 'knowledge_suggestions'")
            )
        ).scalar()
    assert repaired == "", "the row the old default produced is repaired"
    assert "DEFAULT ''" in str(ddl) and "''''''" not in str(ddl)
    row = await SuggestionStore(engine).get(tenant="test", sid="sid1")
    assert row is not None and row.to_path == ""


async def test_revision_0003_adds_the_declared_server_default_to_every_audited_column(
    engine: AsyncEngine,
) -> None:
    """The #834 backfill audit's defensive half: every column that carried a Python-side
    ``default=`` with no ``server_default=`` now carries a matching one at the DB level too.

    Knowledge's own history never left a NULL row behind these columns — every one has been
    part of its table's ``create_table`` since the table's first release (see 0003's docstring)
    — so there is no row to repair here, only the DB-level default this revision adds on top
    of the Python-side one, closing the gap for good.
    """
    await _migrate(engine)

    def defaults(sync_conn: sa.Connection) -> dict[str, dict[str, str | None]]:
        inspector = sa.inspect(sync_conn)
        return {
            table: {col["name"]: col.get("default") for col in inspector.get_columns(table)}
            for table in ("knowledge_suggestions", "knowledge_suggestion_decisions")
        }

    async with engine.connect() as conn:
        result = await conn.run_sync(defaults)

    suggestions = result["knowledge_suggestions"]
    assert suggestions["proposed_content"] == "''"
    assert suggestions["origin"] == "'agent'"
    assert suggestions["note"] == "''"

    decisions = result["knowledge_suggestion_decisions"]
    assert decisions["origin"] == "'agent'"
    assert decisions["note"] == "''"
    assert decisions["proposed_content"] == "''"
    assert decisions["applied_content"] == "''"
    assert decisions["to_path"] == "''"


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
    assert advisory_lock_key(SERVICE) == advisory_lock_key("knowledge")
    assert advisory_lock_key(SERVICE) != advisory_lock_key("storage")
    assert version_table_name(SERVICE) == "alembic_version_knowledge"
