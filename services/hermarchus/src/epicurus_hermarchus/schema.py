"""The SQLite spool's schema, versioned with ``PRAGMA user_version``.

Hermarchus is SQLite-only by design: its database is a spool on the instance's own volume —
sessions, their event logs, the inbox and remembered permissions — not a shared store. The
rest of the stack is Alembic-managed and gated on Postgres (ADR-0138); none of that applies
to a file one process owns, so the schema is versioned the SQLite-native way:

* ``STEPS[n]`` is the ordered DDL that takes a database from version ``n - 1`` to ``n``;
  version 0 is an empty file. Steps are append-only: an existing step is never edited.
* :func:`upgrade` runs every missing step, each in its own transaction together with the
  ``user_version`` bump, so a crash mid-upgrade leaves the previous version intact.
* A database whose version is *newer* than this code is refused, never touched: that is an
  image downgrade, and guessing would corrupt the spool.

The SQLAlchemy tables in :mod:`epicurus_hermarchus.store` are what the code queries; a test
holds them to what these steps create (the drift gate's job in the Alembic services). This
directory deliberately has no ``migrations/env.py``, so ``scripts/migrate.py``'s discovery
glob — and the Postgres ``migrations`` CI job — never pick Hermarchus up.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

__all__ = ["LATEST", "STEPS", "SchemaError", "current_version", "upgrade"]


class SchemaError(RuntimeError):
    """The database cannot be used by this version of Hermarchus."""


STEPS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE sessions (
            id TEXT NOT NULL PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            cwd TEXT NOT NULL,
            title TEXT,
            status TEXT NOT NULL,
            unattended BOOLEAN NOT NULL,
            agent_session_id TEXT,
            last_seq INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE events (
            tenant_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            type TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (session_id, seq)
        )
        """,
        """
        CREATE TABLE inbox (
            id TEXT NOT NULL PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered_at TEXT
        )
        """,
        "CREATE INDEX ix_inbox_session_status ON inbox (session_id, status)",
        """
        CREATE TABLE permission_requests (
            id TEXT NOT NULL PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            status TEXT NOT NULL,
            request TEXT NOT NULL,
            decision TEXT,
            resolved_by TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """,
        "CREATE INDEX ix_permission_requests_session ON permission_requests (session_id, status)",
        """
        CREATE TABLE permission_rules (
            tenant_id TEXT NOT NULL,
            project_dir TEXT NOT NULL,
            agent TEXT NOT NULL,
            tool_kind TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, project_dir, agent, tool_kind, title)
        )
        """,
    ),
}

LATEST = max(STEPS)


async def current_version(conn: AsyncConnection) -> int:
    result = await conn.exec_driver_sql("PRAGMA user_version")
    row = result.first()
    return int(row[0]) if row else 0


async def upgrade(engine: AsyncEngine, steps: Mapping[int, tuple[str, ...]] | None = None) -> int:
    """Bring the database to the latest version; return the version it ended at."""
    plan = STEPS if steps is None else steps
    latest = max(plan) if plan else 0
    async with engine.connect() as conn:
        version = await current_version(conn)
    if version > latest:
        raise SchemaError(
            f"the state database is at schema version {version}, newer than this Hermarchus "
            f"understands ({latest}) — an image downgrade? Restore a matching image or a "
            "backup of the state volume taken before the upgrade."
        )
    for target in range(version + 1, latest + 1):
        if target not in plan:
            raise SchemaError(f"no schema step for version {target}")
        async with engine.begin() as conn:
            for statement in plan[target]:
                await conn.exec_driver_sql(statement)
            # PRAGMA takes no bound parameters; `target` is an int from this module.
            await conn.exec_driver_sql(f"PRAGMA user_version = {int(target)}")
    return latest
