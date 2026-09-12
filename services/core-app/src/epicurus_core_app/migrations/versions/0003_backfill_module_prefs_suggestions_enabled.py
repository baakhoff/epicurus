"""backfill module_prefs suggestions_enabled

Revision 0003, following 0002.

The column the owner named when #834 was decided, and #903's cause. ``module_prefs`` shipped on
2026-06-17; ``suggestions_enabled`` joined it a week later with a Python-side ``default=True``
and no server default. The additive reconcile cannot add a ``NOT NULL`` column to a populated
table without a value to backfill it with, so on every deployment older than that week it was
added **nullable** and existing rows got ``NULL`` (ADR-0067 documents the limit). Readers
coerced it — until a prefs *write* round-tripped the row through Pydantic and a ``NULL`` in a
``bool`` field aborted the whole set (#903, worked around at the portability seam in #914).

A migration can do what the reconcile could not: give the existing rows the model's default,
then make the column what the model has always said it is. The model now also declares
``server_default=text("true")``, so the two paths — a table created fresh and a table the
baseline reconciles — produce the same column, and ``alembic check`` can hold them to it.

Idempotent and harmless on a deployment that never had the ``NULL``s: the ``UPDATE`` matches no
rows and the ``ALTER`` is a no-op restatement of what is already there.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Give the reconciled ``NULL`` rows the model's default, then enforce ``NOT NULL``."""
    # The UPDATE first, so SET NOT NULL has nothing left to refuse. `true` rather than `1`:
    # Postgres needs a boolean, and SQLite has accepted the keyword since 3.23.
    op.execute(
        "UPDATE module_prefs SET suggestions_enabled = true WHERE suggestions_enabled IS NULL"
    )
    # Batch, because SQLite cannot ALTER a column at all; on Postgres this compiles to the
    # plain ALTER TABLE … SET DEFAULT / SET NOT NULL pair.
    with op.batch_alter_table("module_prefs") as batch_op:
        batch_op.alter_column(
            "suggestions_enabled",
            existing_type=sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        )


def downgrade() -> None:
    """Let the column be ``NULL`` again, and drop the server default.

    Deliberately does not restore the ``NULL``s: which rows held one is not recorded anywhere,
    and "on" is the value every reader already gave them.
    """
    with op.batch_alter_table("module_prefs") as batch_op:
        batch_op.alter_column(
            "suggestions_enabled",
            existing_type=sa.Boolean(),
            server_default=None,
            nullable=True,
        )
