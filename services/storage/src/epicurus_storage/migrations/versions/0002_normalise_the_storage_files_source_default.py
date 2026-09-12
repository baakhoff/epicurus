"""normalise the storage_files source default

Revision 0002, following 0001.

``storage_files.source`` was declared ``server_default="'fs'"`` — a *plain string*, which
SQLAlchemy quotes as a literal, so ``create_all`` emitted ``DEFAULT '''fs'''``: a default whose
value is the four characters ``'fs'``, quotes included. The additive reconcile pasted the same
string into ``ALTER TABLE … ADD COLUMN`` as raw SQL and emitted ``DEFAULT 'fs'``, so a database
that created the table fresh and one that gained the column through the reconcile disagreed
about their own default. Nothing noticed, because every insert sets ``source`` explicitly and
the row-reader maps anything that is not ``"object"`` to ``"fs"`` — but a plain SQL insert would
have produced a row whose ``source`` is literally ``'fs'``.

The model now says ``text("'fs'")``, which is what its comment always claimed. This revision
brings an existing database to the same place: the default, and any row the old one produced.
It is the first change in this repository the reconcile could never have made — it alters an
existing column rather than adding one (#834, #926).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Set the default to ``'fs'`` and repair any row the doubled-quote default produced."""
    # Rows first: one holding the literal ``'fs'`` came from the old default and reads back as
    # "fs" only through the row-reader's fallback. Make the stored value honest.
    op.execute("UPDATE storage_files SET source = 'fs' WHERE source = '''fs'''")
    # Inside a batch block, because SQLite has no ALTER COLUMN at all: batch mode rebuilds the
    # table there (copy, move the rows, swap the names) and compiles to the plain ALTER on
    # Postgres, so one revision runs on production and on `task migrate:check` alike.
    with op.batch_alter_table("storage_files") as batch_op:
        batch_op.alter_column(
            "source",
            existing_type=sa.String(length=16),
            existing_nullable=False,
            server_default=sa.text("'fs'"),
        )


def downgrade() -> None:
    """Restore the doubled-quote default. Deliberately does not un-repair the rows."""
    with op.batch_alter_table("storage_files") as batch_op:
        batch_op.alter_column(
            "source",
            existing_type=sa.String(length=16),
            existing_nullable=False,
            server_default=sa.text("'''fs'''"),
        )
