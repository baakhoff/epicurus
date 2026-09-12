"""core-app's migration environment, exercised end to end on SQLite.

The core owns 40 tables across 28 ``DeclarativeBase`` objects — by far the biggest schema in the
repository — and as of #927 every one of them comes from the revisions in
``epicurus_core_app.migrations`` rather than from 29 ``create_all`` + additive-reconcile calls in
the lifespan (#834, ADR-XXXX). Five things are worth proving here:

* the revisions and the models agree — ``upgrade head`` from empty produces exactly what
  ``create_all`` would, table for table and column for column, so the test suite's cheaper
  ``init()`` path is not quietly building a different schema from production's;
* **nothing escapes** ``METADATAS``: a store added later whose base is not in that tuple would be
  a store no revision creates and no drift check sees, and with 28 bases that is an easy mistake
  to make. Two tests scan the package's source for it rather than trusting the list;
* a database built **before** core-app adopted Alembic is adopted correctly — the baseline
  reconciles its existing tables, repairs the columns the additive reconcile was responsible
  for, adds the indexes it never could, and keeps every row;
* the three post-baseline revisions do what they claim on the database state they were written
  for: 0002's doubled-quote defaults, 0003's and 0004's reconciled ``NULL``s;
* running it twice is a no-op, and doing so off Postgres neither takes nor breaks on the
  advisory lock that serialises two replicas.

Everything runs against **file-backed** SQLite under ``tmp_path``: the migration runner opens its
own connections, and the in-memory/``StaticPool`` fixture shares one DBAPI connection across
every session, which is the setup that silently swallows a concurrent writer's commit (AGENTS.md).
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import epicurus_core_app
from epicurus_core.db.migrations import advisory_lock_key, run_migrations, version_table_name
from epicurus_core_app.automations.store import AutomationStore
from epicurus_core_app.migrations import METADATAS, SCRIPT_LOCATION, SERVICE
from epicurus_core_app.module_prefs import ModulePrefsStore

HEAD = "0004"
PACKAGE_ROOT = Path(epicurus_core_app.__file__).resolve().parent


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A file-backed SQLite engine with default pooling, disposed on teardown."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'core.sqlite'}")
    try:
        yield engine
    finally:
        await engine.dispose()


async def _migrate(engine: AsyncEngine) -> str:
    return await run_migrations(
        engine, service=SERVICE, script_location=SCRIPT_LOCATION, metadatas=METADATAS
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


async def _create_all(engine: AsyncEngine) -> None:
    """Build every table from the models — what every store's ``init()`` does, in one pass."""
    async with engine.begin() as conn:
        for metadata in METADATAS:
            await conn.run_sync(metadata.create_all)


async def _scalar(engine: AsyncEngine, sql: str) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(sql))).scalar()


async def _table_ddl(engine: AsyncEngine, table: str) -> str:
    sql = await _scalar(engine, f"SELECT sql FROM sqlite_master WHERE name = '{table}'")
    return str(sql)


# ── Nothing escapes METADATAS ─────────────────────────────────────────────────


def _module_sources() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if "migrations" not in path.parts
    ]


def test_every_declarative_base_in_the_service_is_in_metadatas() -> None:
    """One entry in ``METADATAS`` per ``DeclarativeBase``, counted from the source itself.

    A base left out is a store whose tables no revision creates and whose drift nothing detects —
    and with 28 of them, adding the 29th and forgetting the tuple is the obvious mistake. Counting
    rather than hard-coding 28 means this fails on the commit that adds the base, naming why.
    """
    bases = [
        f"{path.relative_to(PACKAGE_ROOT)}:{node.name}"
        for path, tree in _module_sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "DeclarativeBase" for base in node.bases)
    ]
    assert len(bases) == len(METADATAS), (
        "every DeclarativeBase in epicurus_core_app needs a line in "
        f"migrations/__init__.py's METADATAS; found {len(bases)} bases for "
        f"{len(METADATAS)} metadatas: {bases}"
    )


def test_every_mapped_table_in_the_service_is_covered_by_metadatas() -> None:
    """The stronger form: every ``__tablename__`` in the package is in the migration target.

    Catches the subtler miss the count cannot — a store that hangs its table off *another*
    module's base (``memory.profile`` and ``memory.extraction_queue`` both do) adds no base, so
    the count stays right while the table goes unregistered unless that module is imported.
    """
    declared = {
        node.value.value
        for _, tree in _module_sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    covered = {table for metadata in METADATAS for table in metadata.tables}
    assert declared == covered, (
        "a mapped table is missing from METADATAS (or its module is never imported by "
        f"migrations/__init__.py): {sorted(declared - covered)}"
    )
    assert len(covered) == 40, "the core owns 40 tables; update this count deliberately"


# ── The revisions and the models agree ────────────────────────────────────────


async def test_upgrade_head_from_empty_matches_the_models(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """``upgrade head`` and ``create_all`` produce the same tables and columns.

    The unit tests build schema with ``create_all`` (each store's ``init()``) because an Alembic
    run per test is needless cost; production builds it from the revisions. This is what makes
    those two the same thing, on top of the `migrations` CI gate that re-proves it on Postgres.
    """
    outcome = await _migrate(engine)
    assert outcome == "fresh"
    assert await _version(engine) == HEAD
    migrated = await _shape(engine)

    direct = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'direct.sqlite'}")
    try:
        await _create_all(direct)
        assert migrated == await _shape(direct)
    finally:
        await direct.dispose()
    assert len(migrated) == 40


async def test_a_store_works_against_a_migrated_schema(engine: AsyncEngine) -> None:
    """A round-trip through a store on a schema nothing but the revisions built."""
    await _migrate(engine)
    prefs = ModulePrefsStore(engine)
    await prefs.set_enabled("test", "calendar", False)
    assert await prefs.enabled_map("test") == {"calendar": False}
    assert await prefs.get_suggestions_enabled("test", "calendar") is True


# ── Adopting a database built before Alembic ──────────────────────────────────


async def test_a_pre_alembic_database_is_adopted_and_keeps_its_rows(engine: AsyncEngine) -> None:
    """``create_all``-built tables, no version table: the baseline reconciles rather than fails.

    This is every existing deployment on the release that adopts migrations. The tables are
    already there, so a plain ``op.create_table`` would abort the upgrade on the first of 40;
    the baseline's idempotent ops take the reconcile arm instead, the rows survive, and the
    version table lands at head.
    """
    await _create_all(engine)
    await ModulePrefsStore(engine).set_enabled("test", "notes", False)
    assert await _version(engine) is None

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD
    assert await ModulePrefsStore(engine).enabled_map("test") == {"notes": False}


async def test_the_baseline_repairs_columns_the_old_reconcile_was_responsible_for(
    engine: AsyncEngine,
) -> None:
    """A deployment predating the #128/ADR-0030 columns gains them, backfilled to ``{}``.

    The additive reconcile did this from ``ModulePrefsStore.init()`` (#249, ADR-0067). It is the
    baseline revision's job now, and the behaviour that matters — an existing row reads back as
    an empty mapping rather than ``NULL``, because these columns carry a literal
    ``server_default`` — is unchanged.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE module_prefs ("
            "tenant VARCHAR(63) NOT NULL, module VARCHAR(128) NOT NULL, "
            "enabled BOOLEAN NOT NULL, removed BOOLEAN NOT NULL, "
            "PRIMARY KEY (tenant, module))"
        )
        await conn.exec_driver_sql(
            "INSERT INTO module_prefs (tenant, module, enabled, removed) "
            "VALUES ('test', 'calendar', 1, 0)"
        )

    assert await _migrate(engine) == "adopted"
    columns = (await _shape(engine))["module_prefs"]
    assert {"models", "disabled_tools", "collections", "suggestions_enabled"} <= set(columns)
    prefs = ModulePrefsStore(engine)
    assert await prefs.get_models("test", "calendar") == {}
    assert await prefs.get_disabled_tools("test", "calendar") == set()


async def test_the_baseline_adds_an_index_a_reconciled_table_never_got(
    engine: AsyncEngine,
) -> None:
    """An index belongs to the table's shape too, and the old reconcile never added one."""

    def indexes(sync_conn: sa.Connection) -> set[str]:
        return {ix["name"] or "" for ix in sa.inspect(sync_conn).get_indexes("module_events")}

    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE module_events ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63) NOT NULL, module VARCHAR(128) NOT NULL, "
            "event_type VARCHAR(128) NOT NULL, dedup_key VARCHAR(255) NOT NULL, "
            "payload JSON NOT NULL, schema_version INTEGER NOT NULL, "
            "received_at DATETIME NOT NULL)"
        )
    async with engine.connect() as conn:
        assert await conn.run_sync(indexes) == set()

    await _migrate(engine)
    async with engine.connect() as conn:
        assert "ix_module_events_tenant" in await conn.run_sync(indexes)


# ── The three revisions past the baseline ─────────────────────────────────────


async def test_revision_0002_normalises_a_doubled_quote_default(engine: AsyncEngine) -> None:
    """The first change the additive reconcile could never have made — it *alters* a column.

    ``module_prefs.models`` was declared ``server_default="'{}'"``, a *plain string*, which
    SQLAlchemy quotes as a literal: ``create_all`` wrote ``DEFAULT '''{}'''`` while the reconcile,
    pasting the same string in as raw SQL, wrote ``DEFAULT '{}'``. Two databases, two different
    defaults, and an insert that omitted the column got a value with quotes in it. Nothing
    noticed because every insert sets these columns and the readers fall back on unparseable
    JSON — so this is the state an existing deployment is really in, and 0002 is what fixes it.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE module_prefs ("
            "tenant VARCHAR(63) NOT NULL, module VARCHAR(128) NOT NULL, "
            "enabled BOOLEAN NOT NULL, removed BOOLEAN NOT NULL, "
            "models TEXT DEFAULT '''{}''' NOT NULL, "
            "disabled_tools TEXT DEFAULT '''[]''' NOT NULL, "
            "collections TEXT DEFAULT '''{}''' NOT NULL, "
            "suggestions_enabled BOOLEAN DEFAULT 1 NOT NULL, "
            "PRIMARY KEY (tenant, module))"
        )
        # A row the old default produced: its stored `models` carries the quotes.
        await conn.exec_driver_sql(
            "INSERT INTO module_prefs (tenant, module, enabled, removed) "
            "VALUES ('test', 'calendar', 1, 0)"
        )
    assert await _scalar(engine, "SELECT models FROM module_prefs") == "'{}'", (
        "this test is pointless unless the old default really did this"
    )

    assert await _migrate(engine) == "adopted"
    assert await _version(engine) == HEAD

    assert await _scalar(engine, "SELECT models FROM module_prefs") == "{}"
    ddl = await _table_ddl(engine, "module_prefs")
    assert "DEFAULT '{}'" in ddl and "'''{}'''" not in ddl
    # The table was rebuilt by batch mode; its primary key has to survive that.
    assert await ModulePrefsStore(engine).get_models("test", "calendar") == {}


async def test_revision_0003_backfills_a_reconciled_null_suggestions_enabled(
    engine: AsyncEngine,
) -> None:
    """#903's cause, fixed at the schema rather than coerced at every reader.

    ``suggestions_enabled`` postdates ``module_prefs``, is ``NOT NULL`` in the model and has no
    Python-visible way to backfill a populated table — so the additive reconcile added it
    nullable and pre-existing rows got ``NULL``, which a prefs *write* then choked on. 0003 gives
    those rows the model's default and makes the column ``NOT NULL`` for real.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE module_prefs ("
            "tenant VARCHAR(63) NOT NULL, module VARCHAR(128) NOT NULL, "
            "enabled BOOLEAN NOT NULL, removed BOOLEAN NOT NULL, "
            "models TEXT DEFAULT '{}' NOT NULL, "
            "disabled_tools TEXT DEFAULT '[]' NOT NULL, "
            "collections TEXT DEFAULT '{}' NOT NULL, "
            "suggestions_enabled BOOLEAN, "
            "PRIMARY KEY (tenant, module))"
        )
        await conn.exec_driver_sql(
            "INSERT INTO module_prefs (tenant, module, enabled, removed) "
            "VALUES ('test', 'calendar', 1, 0)"
        )
    assert await _scalar(engine, "SELECT suggestions_enabled FROM module_prefs") is None

    assert await _migrate(engine) == "adopted"

    assert await _scalar(engine, "SELECT suggestions_enabled FROM module_prefs") == 1
    assert "suggestions_enabled" in await _table_ddl(engine, "module_prefs")
    # The write that #903 reported as a 500 — the row no longer carries a NULL to trip over.
    prefs = ModulePrefsStore(engine)
    await prefs.set_suggestions_enabled("test", "calendar", False)
    assert await prefs.get_suggestions_enabled("test", "calendar") is False


async def test_revision_0004_backfills_a_reconciled_null_agent_gated_delivery(
    engine: AsyncEngine,
) -> None:
    """The same position as 0003's column, on ``automations`` (#706 landed after #682)."""
    await _create_all(engine)
    async with engine.begin() as conn:
        # Put the table back in the state the reconcile left it: the column nullable, and a row
        # from before it existed. SQLite cannot ALTER, so rebuild the one column that matters.
        await conn.exec_driver_sql("DROP TABLE automations")
        await conn.exec_driver_sql(
            "CREATE TABLE automations ("
            "pk INTEGER PRIMARY KEY, id VARCHAR(32) NOT NULL, tenant VARCHAR(63) NOT NULL, "
            "name VARCHAR(200) NOT NULL, enabled BOOLEAN NOT NULL, source VARCHAR(80) NOT NULL, "
            "event_trigger JSON, schedule_trigger JSON, prompt TEXT NOT NULL, "
            "model VARCHAR(200), autonomy VARCHAR(20) NOT NULL, sinks JSON NOT NULL, "
            "chat_mode VARCHAR(16) NOT NULL, chat_session_id VARCHAR(128), "
            "rate_cap_per_hour INTEGER NOT NULL, digest_window_minutes INTEGER NOT NULL, "
            "sink_config JSON, created_at DATETIME NOT NULL, last_run_at DATETIME, "
            "last_status VARCHAR(255), agent_gated_delivery BOOLEAN)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO automations (id, tenant, name, enabled, source, prompt, autonomy, "
            "sinks, chat_mode, rate_cap_per_hour, digest_window_minutes, created_at) "
            "VALUES ('a1', 'test', 'old', 1, 'user', 'go', 'notify', '[]', 'rolling', 0, 0, "
            "'2026-01-01 00:00:00')"
        )
    assert await _scalar(engine, "SELECT agent_gated_delivery FROM automations") is None

    assert await _migrate(engine) == "adopted"

    assert await _scalar(engine, "SELECT agent_gated_delivery FROM automations") == 0
    stored = await AutomationStore(engine).get(tenant="test", automation_id="a1")
    assert stored is not None and stored.agent_gated_delivery is False


# ── Restart, and the lock that makes a second replica safe ────────────────────


async def test_a_second_run_is_a_no_op(engine: AsyncEngine) -> None:
    """An ordinary restart: already at head, classified ``managed``, nothing changes."""
    await _migrate(engine)
    before = await _shape(engine)
    assert await _migrate(engine) == "managed"
    assert await _shape(engine) == before
    assert await _version(engine) == HEAD


async def test_running_it_twice_over_is_still_the_same_schema(engine: AsyncEngine) -> None:
    """Three runs in a row — the nearest SQLite can get to two starts overlapping.

    core-app is a singleton in the Helm chart (one replica, an RWO PVC), so the advisory lock is
    belt-and-braces here rather than the guard it is for a module that can scale out. It still
    matters: Compose can start a new container while the old one is shutting down. What must hold
    either way is that a second entry into ``upgrade head`` finds nothing to do rather than
    failing or duplicating work — which is exactly what the Postgres loser of the lock sees once
    it is let through. The lock itself is exercised for real by the `migrations` CI gate.
    """
    assert await _migrate(engine) == "fresh"
    first = await _shape(engine)
    assert await _migrate(engine) == "managed"
    assert await _migrate(engine) == "managed"
    assert await _shape(engine) == first


def test_the_advisory_lock_key_is_stable_for_this_service() -> None:
    """Two processes must derive the same key, so it cannot come from a salted hash."""
    assert advisory_lock_key(SERVICE) == advisory_lock_key("core-app")
    assert advisory_lock_key(SERVICE) != advisory_lock_key("storage")
    # Hyphen folded to an underscore — a hyphen would need quoting in every DDL statement.
    assert version_table_name(SERVICE) == "alembic_version_core_app"
