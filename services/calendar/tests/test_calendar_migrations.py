"""Calendar's migration environment, exercised end to end on SQLite (#834, #928).

Adapted from ``services/storage/tests/test_storage_migrations.py``, the reference the
foundation lane asked every service lane to copy. What matters here, and only here:

* the revisions and the models agree — ``upgrade head`` from empty produces exactly what
  building every store's table straight from the models would, table for table and column
  for column, so the test suite's cheaper ``init()`` path is not quietly building a different
  schema from production's;
* a database built **before** calendar adopted Alembic is adopted correctly — the baseline
  reconciles each existing table, repairs a column and an index the old additive reconcile was
  responsible for, and keeps its rows;
* revision 0002 backfills the seven ``NOT NULL``, ``default=``-only columns the audit found
  (#928) — two of which (``calendar_events.all_day``/``excluded``) can carry a real ``NULL`` on
  a deployment that predates them, and five of which cannot but gain the same server default
  for the database to enforce going forward;
* running it twice is a no-op, and doing so off Postgres neither takes nor breaks on the
  advisory lock that serialises two replicas.

Everything runs against **file-backed** SQLite under ``tmp_path``: the migration runner opens
its own connections, and the in-memory/``StaticPool`` fixture shares one DBAPI connection
across every session, which is the setup that silently swallows a concurrent writer's commit
(see AGENTS.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epicurus_calendar.db import LocalEventStore, _Base
from epicurus_calendar.lead_time_prefs import LeadTimePrefsStore, _LeadTimeBase
from epicurus_calendar.migrations import METADATAS, SCRIPT_LOCATION, SERVICE
from epicurus_calendar.scheduler import FiredMarkerStore, _MarkerBase
from epicurus_calendar.sync_store import CalendarSyncStore, SelfWriteLedger, SyncedEvent, _SyncBase
from epicurus_core.db.migrations import advisory_lock_key, run_migrations, version_table_name

HEAD = "0002"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A file-backed SQLite engine with default pooling, disposed on teardown."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'calendar.sqlite'}")
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


async def _create_all_directly(engine: AsyncEngine) -> None:
    """Build every table straight from the models — what the four stores' ``init()`` used to
    do together before startup called ``run_migrations`` instead (#928)."""
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
        await conn.run_sync(_LeadTimeBase.metadata.create_all)
        await conn.run_sync(_MarkerBase.metadata.create_all)
        await conn.run_sync(_SyncBase.metadata.create_all)


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
    """``upgrade head`` and building straight from the models produce the same schema."""
    outcome = await _migrate(engine)
    assert outcome == "fresh"
    assert await _version(engine) == HEAD
    migrated = await _shape(engine)

    direct = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'direct.sqlite'}")
    try:
        await _create_all_directly(direct)
        assert migrated == await _shape(direct)
    finally:
        await direct.dispose()
    assert set(migrated) == {
        "calendar_events",
        "calendar_lead_time_prefs",
        "calendar_fired_markers",
        "calendar_self_writes",
        "calendar_sync_state",
        "calendar_synced_event",
    }


async def test_the_stores_work_against_a_migrated_schema(engine: AsyncEngine) -> None:
    """A round-trip through every store on a schema nothing but the revisions built."""
    await _migrate(engine)

    events = LocalEventStore(engine)
    created = await events.create_event(
        tenant="test", title="standup", start=datetime.now(UTC), end=datetime.now(UTC)
    )
    assert (await events.get_event(tenant="test", event_id=created.id)) is not None

    lead_prefs = LeadTimePrefsStore(engine)
    await lead_prefs.set_lead_minutes("test", 30)
    assert await lead_prefs.get_lead_minutes("test") == 30

    markers = FiredMarkerStore(engine)
    assert await markers.try_claim(tenant="test", event_id=created.id, marker="starting_soon")

    sync_store = CalendarSyncStore(engine)
    await sync_store.set_state(
        tenant="test", account="google", collection="", sync_token="abc", window_start=None
    )
    assert (await sync_store.get_state(tenant="test", account="google", collection="")) is not None

    ledger = SelfWriteLedger(engine)
    await ledger.record(tenant="test", keys=["event_created|local:1"])
    assert await ledger.peek(tenant="test", key="event_created|local:1")


# ── Adopting a database built before Alembic ──────────────────────────────────


async def test_a_pre_alembic_database_is_adopted_and_keeps_its_rows(engine: AsyncEngine) -> None:
    """``create_all``-built tables, no version table: the baseline reconciles rather than fails.

    This is every existing deployment on the release that adopts migrations. Every table is
    already there, so a plain ``op.create_table`` would abort the upgrade; the baseline's
    idempotent ops take the reconcile arm instead, the rows survive, and the version table
    lands at head.
    """
    await _create_all_directly(engine)
    events = LocalEventStore(engine)
    created = await events.create_event(
        tenant="test", title="old event", start=datetime.now(UTC), end=datetime.now(UTC)
    )
    assert await _version(engine) is None

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD
    entry = await events.get_event(tenant="test", event_id=created.id)
    assert entry is not None, "adopting a database must not lose its rows"


async def test_the_baseline_repairs_a_column_the_old_reconcile_was_responsible_for(
    engine: AsyncEngine,
) -> None:
    """A deployment that predates ``calendar_lead_time_prefs.lead_minutes`` gains it.

    The additive reconcile used to do this from ``LeadTimePrefsStore.init()``. It is the
    baseline revision's job now (``ep.create_table``'s reconcile arm), and the behaviour that
    matters — the column exists and reads back nullable — is unchanged.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE calendar_lead_time_prefs (tenant VARCHAR(63) PRIMARY KEY)"
        )
        await conn.exec_driver_sql("INSERT INTO calendar_lead_time_prefs (tenant) VALUES ('test')")

    assert await _migrate(engine) == "adopted"
    assert "lead_minutes" in (await _shape(engine))["calendar_lead_time_prefs"]
    prefs = LeadTimePrefsStore(engine)
    # NULL lead_minutes falls back to the configured default — the pre-existing behaviour.
    assert await prefs.get_lead_minutes("test") == prefs.default


async def test_the_baseline_adds_the_index_a_reconciled_table_never_got(
    engine: AsyncEngine,
) -> None:
    """An index belongs to the table's shape too, and the old reconcile never added one."""

    def indexes(sync_conn: sa.Connection) -> set[str]:
        return {ix["name"] or "" for ix in sa.inspect(sync_conn).get_indexes("calendar_events")}

    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE calendar_events ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63) NOT NULL, event_id VARCHAR(64) NOT NULL, "
            "title VARCHAR(512) NOT NULL, start_dt DATETIME NOT NULL, end_dt DATETIME NOT NULL, "
            "description TEXT, location VARCHAR(512), all_day BOOLEAN, "
            "recurrence TEXT, recurring_event_id VARCHAR(64), excluded BOOLEAN, "
            "attendees TEXT, timezone VARCHAR(64), created_at DATETIME, "
            "CONSTRAINT uq_calendar_tenant_event UNIQUE (tenant, event_id))"
        )
    async with engine.connect() as conn:
        assert await conn.run_sync(indexes) == set()

    await _migrate(engine)
    async with engine.connect() as conn:
        found = await conn.run_sync(indexes)
    assert "ix_calendar_events_tenant" in found
    assert "ix_calendar_events_recurring_event_id" in found


# ── Revision 0002: the backfill audit (#928) ───────────────────────────────────


async def test_revision_0002_backfills_null_all_day_and_excluded_rows(engine: AsyncEngine) -> None:
    """``all_day``/``excluded`` gained no server default from the old reconcile (#903's shape).

    A deployment that predates either column has the reconcile add it **nullable** — there is
    nothing to backfill an existing row with — so an upgraded deployment can hold a real
    ``NULL`` here, coerced to ``False`` on read. This is the state such a deployment is really
    in, and 0002 both repairs the row and adds the server default the baseline could not.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE calendar_events ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63) NOT NULL, event_id VARCHAR(64) NOT NULL, "
            "title VARCHAR(512) NOT NULL, start_dt DATETIME NOT NULL, end_dt DATETIME NOT NULL, "
            "description TEXT, location VARCHAR(512), all_day BOOLEAN, "
            "recurrence TEXT, recurring_event_id VARCHAR(64), excluded BOOLEAN, "
            "attendees TEXT, timezone VARCHAR(64), created_at DATETIME, "
            "CONSTRAINT uq_calendar_tenant_event UNIQUE (tenant, event_id))"
        )
        await conn.exec_driver_sql(
            "INSERT INTO calendar_events (tenant, event_id, title, start_dt, end_dt) "
            "VALUES ('test', 'evt-1', 'legacy event', '2026-01-01 00:00:00', "
            "'2026-01-01 01:00:00')"
        )
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text("SELECT all_day, excluded FROM calendar_events WHERE event_id = 'evt-1'")
            )
        ).one()
    assert row.all_day is None and row.excluded is None, "the test is pointless without a NULL row"

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD

    async with engine.connect() as conn:
        repaired = (
            await conn.execute(
                sa.text("SELECT all_day, excluded FROM calendar_events WHERE event_id = 'evt-1'")
            )
        ).one()
        ddl = (
            await conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE name = 'calendar_events'")
            )
        ).scalar()
    assert not repaired.all_day and not repaired.excluded, "the legacy row is backfilled to False"
    assert "DEFAULT" in str(ddl)
    events = LocalEventStore(engine)
    entry = await events.get_event(tenant="test", event_id="evt-1")
    assert entry is not None and entry.all_day is False


async def test_revision_0002_sets_the_server_default_on_the_unique_constrained_columns(
    engine: AsyncEngine,
) -> None:
    """``collection``/``title``/``change_hash`` have always been ``NOT NULL`` — nothing to
    backfill, since neither ``create_all`` nor the baseline could ever have left a row without
    a value — but 0002 still gives the database the same server default the model has always
    supplied at the application layer, per the audit rule (#928): decide the value per row
    rather than a blanket constant, which here means the model's own existing default.
    """
    await _create_all_directly(engine)
    sync_store = CalendarSyncStore(engine)
    await sync_store.set_state(
        tenant="test", account="google", collection="", sync_token=None, window_start=None
    )
    await sync_store.upsert_events(
        tenant="test",
        account="google",
        collection="",
        events=[
            SyncedEvent(
                event_id="e1", start=datetime.now(UTC), end=datetime.now(UTC), title="meeting"
            )
        ],
    )

    assert await _migrate(engine) == "adopted"

    async with engine.connect() as conn:
        state_ddl = (
            await conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE name = 'calendar_sync_state'")
            )
        ).scalar()
        event_ddl = (
            await conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE name = 'calendar_synced_event'")
            )
        ).scalar()
    assert "DEFAULT" in str(state_ddl)
    assert "DEFAULT" in str(event_ddl)
    # The rows from before the migration are untouched — no value collided.
    cached = await sync_store.get_events(tenant="test", account="google", collection="")
    assert "e1" in cached


# ── Restart, and the lock that makes a second replica safe ────────────────────


async def test_a_second_run_is_a_no_op(engine: AsyncEngine) -> None:
    """An ordinary restart: already at head, classified ``managed``, nothing changes."""
    await _migrate(engine)
    before = await _shape(engine)
    assert await _migrate(engine) == "managed"
    assert await _shape(engine) == before
    assert await _version(engine) == HEAD


async def test_running_it_twice_over_is_still_the_same_schema(engine: AsyncEngine) -> None:
    """Three runs in a row — the nearest SQLite can get to two replicas starting together."""
    assert await _migrate(engine) == "fresh"
    first = await _shape(engine)
    assert await _migrate(engine) == "managed"
    assert await _migrate(engine) == "managed"
    assert await _shape(engine) == first


def test_the_advisory_lock_key_is_stable_for_this_service() -> None:
    """Two replicas must derive the same key, so it cannot come from a salted hash."""
    assert advisory_lock_key(SERVICE) == advisory_lock_key("calendar")
    assert advisory_lock_key(SERVICE) != advisory_lock_key("storage")
    assert version_table_name(SERVICE) == "alembic_version_calendar"
