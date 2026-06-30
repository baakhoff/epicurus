"""Tests for the additive schema reconcile (``epicurus_core.db.ensure_columns``).

The helper runs on a *sync* connection (stores call it via ``conn.run_sync``), so these
exercise it directly against an in-memory SQLite database. A ``StaticPool`` keeps one
connection so the schema persists across ``begin()`` blocks. Each test builds a "legacy"
table (the original columns only), then reconciles it against a fuller model and asserts
both the resulting schema and that existing rows survive — the drift class this guards.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from epicurus_core.db import ensure_columns


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine("sqlite://", poolclass=StaticPool)
    try:
        yield eng
    finally:
        eng.dispose()


def _columns(eng: Engine, table_name: str) -> dict[str, dict[str, Any]]:
    with eng.connect() as conn:
        return {c["name"]: dict(c) for c in inspect(conn).get_columns(table_name)}


def _make_legacy(eng: Engine, table: Table, *seed_sql: str) -> None:
    """Create *table* (the pre-upgrade subset) and run any seed INSERTs."""
    with eng.begin() as conn:
        table.create(conn)
        for stmt in seed_sql:
            conn.execute(text(stmt))


def test_adds_missing_nullable_column(engine: Engine) -> None:
    legacy = Table(
        "widgets",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("name", String(64)),
    )
    _make_legacy(engine, legacy, "INSERT INTO widgets (id, name) VALUES (1, 'a')")

    model = Table(
        "widgets",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("name", String(64)),
        Column("note", Text, nullable=True),  # introduced after first release
    )
    with engine.begin() as conn:
        ensure_columns(conn, model, ["note"])

    cols = _columns(engine, "widgets")
    assert "note" in cols
    assert cols["note"]["nullable"] is True
    with engine.connect() as conn:
        # the pre-existing row survives and reads NULL for the new column
        assert conn.execute(text("SELECT note FROM widgets WHERE id = 1")).scalar() is None


def test_not_null_column_with_server_default_backfills_existing_rows(engine: Engine) -> None:
    """The storage ``source`` case: ``NOT NULL DEFAULT 'fs'`` backfills old rows to 'fs'."""
    legacy = Table("files", MetaData(), Column("id", Integer, primary_key=True))
    _make_legacy(engine, legacy, "INSERT INTO files (id) VALUES (1)")

    model = Table(
        "files",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("source", String(16), server_default="'fs'", default="fs", nullable=False),
    )
    with engine.begin() as conn:
        ensure_columns(conn, model, ["source"])

    cols = _columns(engine, "files")
    assert cols["source"]["nullable"] is False
    with engine.connect() as conn:
        assert conn.execute(text("SELECT source FROM files WHERE id = 1")).scalar() == "fs"


def test_empty_string_server_default_backfills_existing_rows(engine: Engine) -> None:
    """The knowledge ``to_path`` case: a ``DEFAULT ''`` literal backfills to the empty string.

    Regression guard for the malformed ``server_default=""`` (no quotes) the model used to
    carry — it must be ``server_default="''"`` so the rendered default is a quoted literal.
    """
    legacy = Table("suggestions", MetaData(), Column("id", Integer, primary_key=True))
    _make_legacy(engine, legacy, "INSERT INTO suggestions (id) VALUES (1)")

    model = Table(
        "suggestions",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("to_path", String(4096), server_default="''", default="", nullable=False),
    )
    with engine.begin() as conn:
        ensure_columns(conn, model, ["to_path"])

    cols = _columns(engine, "suggestions")
    assert cols["to_path"]["nullable"] is False
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_path FROM suggestions WHERE id = 1")).scalar() == ""


def test_not_null_without_server_default_is_relaxed_to_nullable(engine: Engine) -> None:
    """The calendar ``all_day`` case: NOT NULL but no default → added nullable, not failing.

    A ``NOT NULL`` add with no value to backfill would fail on a populated table, so the
    helper relaxes the constraint and the row-reader coerces the NULL to the model default.
    """
    legacy = Table("events", MetaData(), Column("id", Integer, primary_key=True))
    _make_legacy(engine, legacy, "INSERT INTO events (id) VALUES (1)")

    model = Table(
        "events",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("all_day", Boolean, default=False, nullable=False),  # NOT NULL, no server_default
    )
    with engine.begin() as conn:
        ensure_columns(conn, model, ["all_day"])  # must not raise

    cols = _columns(engine, "events")
    assert cols["all_day"]["nullable"] is True  # relaxed
    with engine.connect() as conn:
        assert conn.execute(text("SELECT all_day FROM events WHERE id = 1")).scalar() is None


def test_idempotent_skips_existing_columns(engine: Engine) -> None:
    model = Table(
        "widgets",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("note", Text, nullable=True),
    )
    legacy = Table(
        "widgets",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("note", Text, nullable=True),  # already present
    )
    _make_legacy(engine, legacy)

    # Column already exists → no-op; a second pass is also a no-op (safe on every startup).
    with engine.begin() as conn:
        ensure_columns(conn, model, ["note"])
        ensure_columns(conn, model, ["note"])

    assert "note" in _columns(engine, "widgets")


def test_adds_several_columns_in_one_pass(engine: Engine) -> None:
    legacy = Table("tasks_local", MetaData(), Column("id", Integer, primary_key=True))
    _make_legacy(engine, legacy, "INSERT INTO tasks_local (id) VALUES (1)")

    model = Table(
        "tasks_local",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("status", String(32), nullable=True),
        Column("priority", String(16), nullable=True),
        Column("tags", Text, nullable=True),
    )
    with engine.begin() as conn:
        ensure_columns(conn, model, ["status", "priority", "tags"])

    cols = _columns(engine, "tasks_local")
    assert {"status", "priority", "tags"} <= set(cols)


def test_rejects_a_non_table(engine: Engine) -> None:
    """The guard rejects a FromClause that is not a Table (e.g. a subquery) — a misuse."""
    table = Table("x", MetaData(), Column("id", Integer, primary_key=True))
    subquery = select(table).subquery()
    with engine.begin() as conn, pytest.raises(TypeError):
        ensure_columns(conn, subquery, ["id"])
