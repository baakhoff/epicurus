"""Notes' migration environment, exercised end to end on SQLite (#834, #930).

Notes follows the shape storage set as the reference (#926): the properties worth proving
here are that the revisions and the models agree, that a database built **before** notes
adopted Alembic is adopted without losing rows, and that a restart is a no-op.

Two of storage's test shapes have no analogue here and are deliberately not copied: notes has
never called ``ensure_columns`` (the additive reconcile), so there is no column or index this
service's baseline needs to *repair* on an existing table — every table and every index it owns
arrived together with the table (see the backfill audit in the PR body and in
``docs/services/notes.md``). What that test would prove — a `create_all`-built table gaining
something the old reconcile owed it — simply never happened here.

Everything runs against **file-backed** SQLite under ``tmp_path``: the migration runner opens
its own connections, and the in-memory/``StaticPool`` fixture shares one DBAPI connection across
every session, which is the setup that silently swallows a concurrent writer's commit (AGENTS.md).
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
from epicurus_notes.db import NoteFolderStore, NotesStore, _Base
from epicurus_notes.migrations import METADATAS, SCRIPT_LOCATION, SERVICE
from epicurus_notes.suggestions import (
    NoteSuggestionAuditStore,
    NoteSuggestionStore,
    _NoteAuditBase,
    _NoteSuggestionBase,
)

HEAD = "0001"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A file-backed SQLite engine with default pooling, disposed on teardown."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notes.sqlite'}")
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


async def _create_all_direct(engine: AsyncEngine) -> None:
    """Build every table straight from the models — what the four stores' ``init()`` did."""
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
        await conn.run_sync(_NoteSuggestionBase.metadata.create_all)
        await conn.run_sync(_NoteAuditBase.metadata.create_all)


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
    """``upgrade head`` and the four stores' ``create_all`` produce the same tables/columns.

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
        await _create_all_direct(direct)
        assert migrated == await _shape(direct)
    finally:
        await direct.dispose()
    assert {
        "notes",
        "note_folders",
        "note_versions",
        "notes_suggestions",
        "notes_suggestion_decisions",
    } <= set(migrated)


async def test_the_stores_work_against_a_migrated_schema(engine: AsyncEngine) -> None:
    """A round-trip through each store on a schema nothing but the revisions built."""
    await _migrate(engine)
    notes = NotesStore(engine)
    folders = NoteFolderStore(engine)
    suggestions = NoteSuggestionStore(engine)
    audit = NoteSuggestionAuditStore(engine)

    await notes.upsert(tenant="test", slug="a", title="A", content="hello")
    saved = await notes.get(tenant="test", slug="a")
    assert saved is not None and saved.content == "hello"

    assert await folders.add(tenant="test", path="work") is True
    assert await folders.list(tenant="test") == ["work"]

    suggestion = await suggestions.add(
        tenant="test",
        slug="a",
        operation="update",
        proposed_content="hello, revised",
        origin="agent",
        note="",
    )
    # The 7 default=-without-server_default= columns (see the PR body's backfill audit) are
    # filled in by the ORM at flush, exactly as they always were before this service adopted
    # migrations — the point this round trip pins.
    assert suggestion.origin == "agent"

    await audit.record(
        tenant="test",
        sid=suggestion.sid,
        slug="a",
        operation="update",
        origin="agent",
        note="",
        proposed_at=suggestion.created_at,
        decision="approved",
        proposed_content="hello, revised",
        applied_content="hello, revised",
    )
    decisions = await audit.list(tenant="test")
    assert len(decisions) == 1 and decisions[0].applied_content == "hello, revised"


# ── Adopting a database built before Alembic ──────────────────────────────────


async def test_a_pre_alembic_database_is_adopted_and_keeps_its_rows(engine: AsyncEngine) -> None:
    """``create_all``-built tables, no version table: the baseline reconciles rather than fails.

    This is every existing deployment on the release that adopts migrations. Every table this
    service owns is already there, so a plain ``op.create_table`` would abort the upgrade; the
    baseline's idempotent ops take the reconcile arm instead, the rows survive, and the version
    table lands at head.
    """
    await _create_all_direct(engine)
    await NotesStore(engine).upsert(tenant="test", slug="old", title="Old", content="body")
    assert await _version(engine) is None

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD
    entry = await NotesStore(engine).get(tenant="test", slug="old")
    assert entry is not None, "adopting a database must not lose its rows"


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
    lock, so the runner skips it. What must hold either way is that a second entry into
    ``upgrade head`` finds nothing to do rather than failing or duplicating work. The lock
    itself is exercised for real by the `migrations` CI gate.
    """
    assert await _migrate(engine) == "fresh"
    first = await _shape(engine)
    assert await _migrate(engine) == "managed"
    assert await _migrate(engine) == "managed"
    assert await _shape(engine) == first


def test_the_advisory_lock_key_is_stable_for_this_service() -> None:
    """Two replicas must derive the same key, so it cannot come from a salted hash."""
    assert advisory_lock_key(SERVICE) == advisory_lock_key("notes")
    assert advisory_lock_key(SERVICE) != advisory_lock_key("storage")
    assert version_table_name(SERVICE) == "alembic_version_notes"
