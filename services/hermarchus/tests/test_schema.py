"""The SQLite spool's schema versioning (``PRAGMA user_version``), and its drift gate.

Hermarchus is outside the Alembic / Postgres migration framework on purpose (its database is
a per-instance SQLite spool); these tests are what that framework's gate is for the other
services: fresh install, restart no-op, ordered upgrade, atomicity, downgrade refusal, and the
tables the code queries matching what the steps create.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect

from epicurus_hermarchus import schema
from epicurus_hermarchus.store import make_engine, metadata

REPO = Path(__file__).resolve().parents[3]


async def _version(path: Path) -> int:
    engine = make_engine(path)
    try:
        async with engine.connect() as conn:
            return await schema.current_version(conn)
    finally:
        await engine.dispose()


async def _tables(path: Path) -> set[str]:
    engine = make_engine(path)
    try:
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        return set(names)
    finally:
        await engine.dispose()


async def test_a_fresh_database_reaches_the_latest_version(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    engine = make_engine(db)
    try:
        assert await schema.upgrade(engine) == schema.LATEST
    finally:
        await engine.dispose()
    assert await _version(db) == schema.LATEST
    assert {"sessions", "events", "inbox", "permission_requests", "permission_rules"} <= (
        await _tables(db)
    )


async def test_a_second_upgrade_is_a_no_op(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    for _ in range(2):
        engine = make_engine(db)
        try:
            await schema.upgrade(engine)
        finally:
            await engine.dispose()
    assert await _version(db) == schema.LATEST


async def test_steps_run_in_order_from_the_current_version(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    plan = {1: ("CREATE TABLE a (x INTEGER)",), 2: ("ALTER TABLE a ADD COLUMN y TEXT",)}
    engine = make_engine(db)
    try:
        await schema.upgrade(engine, {1: plan[1]})
        assert await schema.upgrade(engine, plan) == 2
        async with engine.connect() as conn:
            cols = await conn.run_sync(
                lambda c: [col["name"] for col in inspect(c).get_columns("a")]
            )
        assert cols == ["x", "y"]
    finally:
        await engine.dispose()


async def test_a_failing_step_leaves_the_previous_version_intact(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    plan = {
        1: ("CREATE TABLE a (x INTEGER)",),
        2: ("CREATE TABLE half (x INTEGER)", "THIS IS NOT SQL"),
    }
    engine = make_engine(db)
    try:
        with pytest.raises(Exception, match="syntax"):
            await schema.upgrade(engine, plan)
    finally:
        await engine.dispose()
    assert await _version(db) == 1
    assert "half" not in await _tables(db), "a half-applied step survived its failure"


async def test_a_newer_database_is_refused_untouched(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    engine = make_engine(db)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(f"PRAGMA user_version = {schema.LATEST + 1}")
        with pytest.raises(schema.SchemaError, match="newer"):
            await schema.upgrade(engine)
    finally:
        await engine.dispose()
    assert await _tables(db) == set()


async def test_the_queried_tables_match_what_the_steps_create(tmp_path: Path) -> None:
    """The drift gate: store.py's Table objects against the database the steps build."""
    db = tmp_path / "s.sqlite3"
    engine = make_engine(db)
    try:
        await schema.upgrade(engine)

        def _reflect(conn: Any) -> dict[str, Any]:
            insp = inspect(conn)
            return {
                name: (
                    [(c["name"], c["nullable"]) for c in insp.get_columns(name)],
                    sorted(insp.get_pk_constraint(name)["constrained_columns"]),
                )
                for name in insp.get_table_names()
            }

        async with engine.connect() as conn:
            actual = await conn.run_sync(_reflect)
    finally:
        await engine.dispose()
    expected = {
        table.name: (
            [(c.name, bool(c.nullable) and not c.primary_key) for c in table.columns],
            sorted(c.name for c in table.primary_key.columns),
        )
        for table in metadata.sorted_tables
    }
    # SQLite reports a non-INTEGER primary-key column as nullable unless declared NOT NULL;
    # every key column here is declared NOT NULL, so the comparison is exact.
    assert actual == expected


def test_hermarchus_stays_out_of_the_postgres_migration_discovery() -> None:
    """`scripts/migrate.py` globs services/*/src/*/migrations/env.py; we must not match."""
    assert not list((REPO / "services" / "hermarchus").rglob("migrations/env.py"))
