"""Additive schema reconcile for stores that have no migration framework.

epicurus services evolve their Postgres schema with ``Base.metadata.create_all``,
which creates a *missing* table but never alters an *existing* one. So any column
added to a model after that table's first release silently never reaches an
already-provisioned database, and every query that references the new column fails on
Postgres with ``column ... does not exist`` — the bug that hit ``llm_prefs`` (#214)
and ``tasks_local`` (#218). The fix was a per-store ``_ensure_columns`` helper, but it
was copy-pasted across nine stores and invisible to CI (the SQLite unit tests and the
runtime-smoke gate always build tables fresh, so drift only shows on a real upgraded
deployment). This module promotes that helper into one shared, audited reconcile every
store calls from ``init()`` after ``create_all`` (#249, ADR-0067).

It is **additive only**: it adds columns that exist on the ORM model but not yet in the
live table. It never drops, renames, retypes, or backfills — those need a real migration
(Alembic), which the project has deliberately not adopted yet. A reconciled column
reproduces the model's type and, when the model declares a ``server_default``, its
``NOT NULL`` constraint and default, so a column looks the same whether the table was
freshly created or reconciled. The one exception: a column the model marks ``NOT NULL``
but gives *no* server default cannot be added to a populated table (there is nothing to
backfill the existing rows with), so it is added **nullable** and the row-reader coerces
the resulting ``NULL`` to the model's Python-side default.

This module lives in the shared library but is intentionally **not** re-exported from
``epicurus_core`` — importing it pulls in SQLAlchemy (the optional ``db`` extra), which
modules without a database should not have to carry. Stores import it directly::

    from epicurus_core.db import ensure_columns
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import Column, FromClause, Table, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.schema import DefaultClause

from epicurus_core.logging import get_logger

__all__ = ["ensure_columns"]

log = get_logger("epicurus_core.db")


def ensure_columns(sync_conn: Connection, table: FromClause, columns: Iterable[str]) -> None:
    """Add any of *columns* the model defines but the live *table* still lacks.

    Call from a store's ``init()`` inside ``conn.run_sync`` after ``create_all``::

        async def init(self) -> None:
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.run_sync(
                    lambda c: ensure_columns(c, _Row.__table__, _ADDED_COLUMNS)
                )

    *table* is a mapped class's ``__table__`` (typed ``FromClause`` but always a ``Table``).
    *columns* names the model columns introduced after the table's first release — the store
    owns that list. Idempotent: a column already present is skipped, so it is safe to run on
    every startup. The column type is compiled for the live dialect, so the same call is
    portable across Postgres (production) and SQLite (tests).
    """
    # A mapped class's ``__table__`` is typed ``FromClause`` but is always a ``Table`` at
    # runtime; narrow it so we can read column metadata (and reject a non-Table misuse).
    if not isinstance(table, Table):
        raise TypeError(f"ensure_columns expects a Table, got {type(table).__name__}")
    inspector = inspect(sync_conn)
    existing = {col["name"] for col in inspector.get_columns(table.name)}
    for name in columns:
        if name in existing:
            continue
        ddl = _add_column_ddl(table.c[name], sync_conn)
        sync_conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {ddl}")
        log.info("reconciled table: added missing column", table=table.name, column=name)


def _add_column_ddl(column: Column[Any], sync_conn: Connection) -> str:
    """Render the ``ADD COLUMN`` body (``name type [NOT NULL] [DEFAULT ...]``) for *column*."""
    type_sql = column.type.compile(dialect=sync_conn.dialect)
    ddl = f"{column.name} {type_sql}"
    default_sql = _server_default_sql(column)
    if default_sql is not None:
        # A server default lets us satisfy a NOT NULL constraint — it backfills the
        # existing rows — so reproduce both and the reconciled column matches create_all.
        if not column.nullable:
            ddl += " NOT NULL"
        ddl += f" DEFAULT {default_sql}"
    # No server default: add the column nullable even when the model marks it NOT NULL.
    # Without a value to backfill, a NOT NULL add fails on a populated table, so we relax
    # the constraint here and the row-reader coerces the NULL to the model's default.
    return ddl


def _server_default_sql(column: Column[Any]) -> str | None:
    """The raw SQL text of *column*'s server default, or ``None`` if it has none.

    Only literal/text server defaults are reconciled — the post-release additive columns
    use string literals like ``'fs'`` or ``'{}'``. A function default such as
    ``func.now()`` (only ever on original ``created_at`` columns, never in an added-column
    list) is ignored, so such a column is added nullable.
    """
    default = column.server_default
    if not isinstance(default, DefaultClause):
        return None
    arg = default.arg
    # A string server_default is stored either as raw text or wrapped in a TextClause
    # (which carries the SQL on ``.text``); accept both, ignore anything else.
    text = getattr(arg, "text", None)
    if isinstance(text, str) and text:
        return text
    if isinstance(arg, str) and arg:
        return arg
    return None
