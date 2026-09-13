"""The two idempotent operations a **baseline** revision is rendered against.

A baseline revision is generated from the models as they stand the day a service adopts
Alembic, so it describes a schema that, on every already-running deployment, *already
exists* — built by the ``create_all`` + reconcile path this framework replaces (ADR-0067).
A plain ``op.create_table`` would therefore fail on exactly the databases that matter most.

Two ways out of that exist. One is to stamp the baseline as applied and upgrade from there;
that needs a separate "is this a pre-Alembic database?" branch at startup, and it is wrong
the moment a deployment skips the adoption release — the stamp would swallow every revision
in between, including a backfill the stamp cannot perform. The other is to make the
*baseline itself* idempotent, which is what this module does: one ``upgrade head`` then
serves an empty database, a database built by the old reconcile, and a database already at
head, with no branch anywhere and no revision ever skipped (ADR-XXXX).

Only the baseline is rendered against these. Every revision after it is ordinary Alembic —
``op.add_column``, ``op.alter_column``, an ``UPDATE`` for a backfill — because after
adoption the database's state is known exactly.

The rendered call style comes from the generator passing ``alembic_module_prefix="ep."`` to
Alembic's code renderer (see ``scripts/migrate.py``), so a baseline reads::

    from epicurus_core.db import ops as ep

    def upgrade() -> None:
        ep.create_table(
            "storage_files",
            sa.Column("id", sa.Integer(), nullable=False),
            ...
        )
        ep.create_index("ix_storage_files_tenant", "storage_files", ["tenant"], unique=False)

From an empty database the renderer only ever emits ``create_table``, ``create_index`` and
``f`` (Alembic's "this name is already final" marker), so those three names are the whole
surface. If a generated baseline references any other ``ep.`` name, the generator fails
loudly rather than shipping a revision this module cannot make idempotent.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.elements import conv

from epicurus_core.db.reconcile import ensure_columns
from epicurus_core.logging import get_logger

__all__ = ["create_index", "create_table", "f"]

log = get_logger("epicurus_core.db.ops")


def f(name: str) -> conv:
    """``op.f`` verbatim — "this name is final, do not apply a naming convention to it".

    Nothing idempotent about it; it is here only because the renderer prefixes *every* call in
    the generated baseline, so an index name arrives as ``ep.f("ix_…")``. Re-exporting it
    keeps the generator's "an unknown ``ep.`` name is a hard error" check meaningful.
    """
    return op.f(name)


def create_table(table_name: str, *columns: Any, **kw: Any) -> None:
    """Create *table_name*, or reconcile its columns if it is already there.

    The create arm is a plain ``op.create_table`` — on an empty database this is an ordinary
    baseline. The reconcile arm runs the audited additive reconcile
    (:func:`epicurus_core.db.reconcile.ensure_columns`) over the columns declared here, which
    adds whatever the live table lacks with the same rules the pre-Alembic path used: the
    model's type, and its ``NOT NULL``/``DEFAULT`` when it declares a literal server default,
    else nullable so the add succeeds on a populated table.

    What the reconcile arm deliberately does **not** do is add a constraint or alter a column
    that is already present: a table that exists but carries, say, no unique constraint is
    beyond an additive repair, and inventing a ``DROP``/``ALTER`` here would make the baseline
    destructive on databases nobody has inspected. Such a case gets its own revision after the
    baseline, where it is explicit and reviewable. Standalone indexes *are* repaired, by
    :func:`create_index` below.
    """
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table_name):
        op.create_table(table_name, *columns, **kw)
        return
    # The table predates this service's adoption of Alembic. Reconcile it instead, using the
    # very columns this baseline declares as the target shape. The Column objects are
    # unattached (the revision constructed them inline), so binding them to a throwaway Table
    # is safe — and is what ensure_columns needs to read their type and server default.
    table = sa.Table(table_name, sa.MetaData(), *columns)
    log.info(
        "baseline met an existing table: reconciling instead of creating",
        table=table_name,
    )
    ensure_columns(bind, table, [column.name for column in table.columns])


def create_index(
    index_name: str, table_name: str, columns: list[str], *, unique: bool = False, **kw: Any
) -> None:
    """Create the index unless *table_name* already carries one by that name.

    The pre-Alembic path created an index only when it created the table, so a deployment
    that gained an indexed column through the additive reconcile has the column and not the
    index. This closes that gap as part of the baseline. A column the reconcile could not add
    at all means the index has nothing to cover, so the create is skipped with a warning
    rather than failing the whole upgrade on a database the reconcile already flagged.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):  # pragma: no cover - create_table ran first
        raise RuntimeError(f"cannot index missing table {table_name!r}")
    if index_name in {index["name"] for index in inspector.get_indexes(table_name)}:
        return
    live = {column["name"] for column in inspector.get_columns(table_name)}
    missing = [column for column in columns if column not in live]
    if missing:
        log.warning(
            "skipping index on columns the reconcile could not add",
            index=index_name,
            table=table_name,
            missing=missing,
        )
        return
    op.create_index(index_name, table_name, columns, unique=unique, **kw)
