"""The shared ``env.py`` body — every service's migration environment, configured once.

Alembic insists on an ``env.py`` inside each script location, and a service needs its own
script location because it owns its own ``versions/`` sequence. What it does **not** need is
its own opinion about how migrations are configured, so each service's ``env.py`` is::

    from epicurus_core.db.alembic_env import run

    run()

and everything that matters — the per-service version table, batch rendering, the type and
server-default comparison the drift gate relies on, and the filter that keeps one service's
autogenerate from seeing the six other services' tables in the shared database — lives here,
in one reviewable place (ADR-XXXX).

This runs **online only**, against a live connection handed over in ``config.attributes``.
There is no offline (``--sql``) arm: the startup path and the CI gate both have a connection,
and an offline render could not consult the live table shape the idempotent baseline needs.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context
from alembic.runtime.environment import NameFilterParentNames, NameFilterType
from sqlalchemy import MetaData
from sqlalchemy.engine import Connection

__all__ = ["run"]


def run() -> None:
    """Configure the migration context from ``config.attributes`` and run the migrations.

    Three attributes are required, and :func:`epicurus_core.db.migrations.alembic_config`
    is the only thing that sets them:

    ``connection``
        A live **sync** :class:`~sqlalchemy.engine.Connection`. Async callers get one via
        ``await async_conn.run_sync(...)``, so no second, synchronous database driver has to
        be installed alongside asyncpg.
    ``metadatas``
        The service's ``MetaData`` objects — one per ``DeclarativeBase``, of which a service
        has as many as it has store modules. Autogenerate and ``alembic check`` compare
        against the lot.
    ``version_table``
        ``alembic_version_<service>``. Every service shares one Postgres database and owns
        disjoint tables in it, so a single ``alembic_version`` would have seven services
        fighting over one head revision.
    """
    config = context.config
    connection = config.attributes.get("connection")
    metadatas = config.attributes.get("metadatas")
    version_table = config.attributes.get("version_table")
    if not isinstance(connection, Connection):
        raise RuntimeError(
            "epicurus migrations run online only: config.attributes['connection'] must be a "
            "live sqlalchemy Connection (use epicurus_core.db.migrations.run_migrations)"
        )
    if not metadatas or not version_table:
        raise RuntimeError(
            "config.attributes must carry 'metadatas' and 'version_table' "
            "(use epicurus_core.db.migrations.alembic_config)"
        )

    owned = _owned_tables(metadatas, version_table)

    def include_name(
        name: str | None, type_: NameFilterType, parent_names: NameFilterParentNames
    ) -> bool:
        """Hide every table this service does not own from autogenerate and ``alembic check``.

        All services share one database, so an unfiltered compare would see the other six
        services' tables as "in the database but not in my models" and propose dropping them —
        and ``alembic check`` would never pass. ``name is None`` is Alembic asking about the
        default schema itself, which is always ours to look at.
        """
        if type_ == "table":
            return name is None or name in owned
        return True

    context.configure(
        connection=connection,
        target_metadata=list(metadatas),
        version_table=version_table,
        # SQLite cannot ALTER much of anything, so a revision that alters a column has to be
        # rendered inside a batch block (copy to a new table, move the rows, swap the names).
        # Rendering every revision that way keeps one revision sequence runnable on both
        # Postgres (production) and SQLite (the local `task migrate:check`); on Postgres batch
        # mode compiles down to the plain ALTER it would have emitted anyway.
        render_as_batch=True,
        # Both comparisons on: the drift gate's whole job is to fail when a model and its
        # revisions disagree, and a silently-widened column or a changed default is exactly
        # the drift that reaches production as a runtime error (#214, #218).
        compare_type=True,
        compare_server_default=True,
        # Nothing here uses a non-default schema; looking for them would only surface the
        # other tenants' or services' objects the filter above exists to hide.
        include_schemas=False,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def _owned_tables(metadatas: Sequence[MetaData], version_table: str) -> frozenset[str]:
    """Every table name this service owns, plus its own Alembic bookkeeping table."""
    names = {table.name for metadata in metadatas for table in metadata.sorted_tables}
    return frozenset(names | {version_table})
