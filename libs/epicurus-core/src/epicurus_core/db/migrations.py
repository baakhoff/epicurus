"""Run a service's schema migrations at startup — the one call a migrated service makes.

``Base.metadata.create_all`` plus the additive reconcile (ADR-0067) carried epicurus to this
point and cannot carry it further: it adds a column and nothing else — no rename, no retype,
no backfill, no drop — so a schema change that needs any of those has been un-shippable, and
a column added ``NOT NULL`` without a server default reaches existing rows as ``NULL`` (#903).
Alembic replaces it, one migration environment per service (#834, ADR-XXXX).

Three facts about this deployment shape determine the design:

* **One database, seven services, disjoint tables.** Each service therefore needs its own
  version table (``alembic_version_<service>``) — one shared ``alembic_version`` would have
  every service overwriting the others' head revision — and each service's autogenerate must
  be blind to the other services' tables (:mod:`epicurus_core.db.alembic_env` does that).
* **One engine, many ``DeclarativeBase`` objects.** A service has a private base per store
  module (28 of them in ``core-app``), so the migration target is a *list* of ``MetaData``,
  and a service must be able to hand that list over without importing its whole app — which
  is what each service's ``<package>.migrations`` module is for.
* **More than one instance may start at once.** The Helm chart can scale a module past one
  replica and Compose can start a new container while the old one is still coming up, so two
  processes can reach ``upgrade head`` together. A Postgres session-level advisory lock keyed
  on the service name serialises them; the loser waits and then finds nothing to do. On
  SQLite (the unit tests) there is no such lock and no such concurrency, so it is skipped.

There is deliberately **no** "is this a pre-Alembic database?" branch. The baseline revision
is itself idempotent (:mod:`epicurus_core.db.ops`), so ``upgrade head`` is the whole algorithm
for an empty database, for one built by the old reconcile, and for one already at head. What
this module still does is *recognise* which of those it found and log it, because an operator
reading a startup log wants to know whether a migration just adopted a years-old database.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import MetaData
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epicurus_core.logging import get_logger

__all__ = [
    "MigrationOutcome",
    "advisory_lock_key",
    "alembic_config",
    "run_migrations",
    "version_table_name",
]

log = get_logger("epicurus_core.db.migrations")

#: Which of the three starting states ``run_migrations`` found, for logging and for tests.
#:
#: ``fresh``    — no version table and none of the service's tables: a new deployment.
#: ``adopted``  — the service's tables are there but no version table: a database built by
#:                the pre-Alembic ``create_all``/reconcile path, now coming under management.
#: ``managed``  — a version table exists; this is an ordinary upgrade (usually a no-op).
MigrationOutcome = Literal["fresh", "adopted", "managed"]

#: Namespace for the advisory-lock key, so the hash can never collide with another
#: subsystem's use of Postgres advisory locks in the same database.
_LOCK_NAMESPACE = "epicurus:db:migrations:"


def version_table_name(service: str) -> str:
    """``alembic_version_<service>`` — this service's private head-revision bookkeeping.

    Hyphens become underscores (``core-app`` → ``alembic_version_core_app``) so the name needs
    no quoting in SQL.
    """
    return f"alembic_version_{service.replace('-', '_')}"


def advisory_lock_key(service: str) -> int:
    """A stable signed 64-bit Postgres advisory-lock key for *service*.

    ``pg_advisory_lock`` takes a ``bigint``, so the service name is hashed into one. BLAKE2b
    rather than :func:`hash`: the key must be identical in every process and across restarts,
    and Python's built-in string hash is salted per interpreter — two replicas would derive
    different keys and neither would ever wait for the other.
    """
    digest = hashlib.blake2b(f"{_LOCK_NAMESPACE}{service}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def alembic_config(
    *,
    script_location: Path,
    metadatas: Sequence[MetaData],
    version_table: str,
    connection: Connection | None = None,
) -> Config:
    """Build the Alembic ``Config`` programmatically — there is no ``alembic.ini`` anywhere.

    Two reasons there isn't. A per-service ini would be seven copies of the same six lines,
    each a place for one service to drift from the others; and the runtime configuration that
    actually matters (the connection, the metadata list, the version table) cannot live in a
    static file at all — it is passed through ``Config.attributes``, which
    :mod:`epicurus_core.db.alembic_env` reads. The file would buy nothing but the ability to
    type ``alembic`` directly, and autogenerate — the one thing that would want — needs a live
    Postgres that a developer is not expected to run (see ``task migrate:new``).
    """
    config = Config()
    config.set_main_option("script_location", str(script_location))
    # Revision files are named NNNN_<slug>.py: a zero-padded sequence, not a hash, so the
    # versions directory reads in application order and a reviewer can see at a glance which
    # revision is new. `alembic history` is still the authority on the actual order (the
    # down_revision chain), but the filenames no longer fight it.
    config.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    config.attributes["metadatas"] = list(metadatas)
    config.attributes["version_table"] = version_table
    if connection is not None:
        config.attributes["connection"] = connection
    return config


async def run_migrations(
    engine: AsyncEngine,
    *,
    service: str,
    script_location: Path,
    metadatas: Sequence[MetaData],
) -> MigrationOutcome:
    """Bring *service*'s schema to head, and report which starting state it found.

    Call this once, from the service's lifespan, **before** anything touches a table::

        from epicurus_core.db.migrations import run_migrations
        from epicurus_storage.migrations import METADATAS, SCRIPT_LOCATION

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            await run_migrations(
                engine,
                service=MODULE_NAME,
                script_location=SCRIPT_LOCATION,
                metadatas=METADATAS,
            )
            ...

    The lifespan, not a separate init step: a container has exactly one entry point on both
    runtimes this stack supports — Compose has no init-container concept, and giving the Helm
    chart one would put the schema behind a code path Compose never executes, which is the
    opposite of the parity rule. One in-process call runs identically on both, and the
    advisory lock is what makes that safe when the chart scales a module past one replica.

    Alembic is driven through ``connection.run_sync``, so the service's existing asyncpg
    engine is the only database driver it needs.
    """
    key = advisory_lock_key(service)
    lock = await _acquire(engine, service=service, key=key)
    try:
        async with engine.begin() as conn:
            outcome: MigrationOutcome = await conn.run_sync(
                _upgrade_to_head,
                service,
                script_location,
                list(metadatas),
            )
    finally:
        await _release(lock, key=key)
    return outcome


def _upgrade_to_head(
    sync_conn: Connection,
    service: str,
    script_location: Path,
    metadatas: list[MetaData],
) -> MigrationOutcome:
    """Classify the database, then ``upgrade head`` — the whole of it, on a sync connection."""
    # Imported here rather than at module scope: `alembic.command` pulls in the script
    # directory machinery, and this module is also imported for `version_table_name` alone.
    from alembic import command

    version_table = version_table_name(service)
    outcome = _classify(sync_conn, metadatas=metadatas, version_table=version_table)
    config = alembic_config(
        script_location=script_location,
        metadatas=metadatas,
        version_table=version_table,
        connection=sync_conn,
    )
    command.upgrade(config, "head")
    log.info(
        "schema migrations applied",
        service=service,
        database=outcome,
        version_table=version_table,
        dialect=sync_conn.dialect.name,
    )
    if outcome == "adopted":
        log.info(
            "adopted a pre-migration database: the baseline reconciled the existing tables "
            "and every later revision applied in order",
            service=service,
        )
    return outcome


def _classify(
    sync_conn: Connection, *, metadatas: Sequence[MetaData], version_table: str
) -> MigrationOutcome:
    """Which of the three starting states this database is in (see :data:`MigrationOutcome`)."""
    present = set(sa.inspect(sync_conn).get_table_names())
    if version_table in present:
        return "managed"
    owned = {table.name for metadata in metadatas for table in metadata.sorted_tables}
    return "adopted" if owned & present else "fresh"


async def _acquire(engine: AsyncEngine, *, service: str, key: int) -> AsyncConnection | None:
    """Take the session-level advisory lock, on a connection of its own. ``None`` off Postgres.

    Its own connection on purpose: the lock must outlive every transaction Alembic opens, and
    sharing the connection Alembic runs on would tie the lock's lifetime to that transaction's
    and put a ``SELECT`` in front of the migration context's own ``BEGIN``.
    """
    if engine.dialect.name != "postgresql":
        log.debug(
            "advisory lock skipped: not Postgres",
            service=service,
            dialect=engine.dialect.name,
        )
        return None
    conn = await engine.connect()
    log.info("waiting for the migration advisory lock", service=service)
    await conn.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": key})
    await conn.commit()
    return conn


async def _release(lock: AsyncConnection | None, *, key: int) -> None:
    """Release the advisory lock and close its connection. A no-op when there was none."""
    if lock is None:
        return
    try:
        await lock.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        await lock.commit()
    finally:
        await lock.close()
