"""backfill knowledge_suggestions and knowledge_suggestion_decisions text defaults

Revision 0003, following 0002.

The #834 backfill audit's other shape (#903): eight columns across these two tables carried a
Python-side ``default=`` but no ``server_default=`` — ``knowledge_suggestions.{proposed_content,
origin, note}`` and ``knowledge_suggestion_decisions.{origin, note, proposed_content,
applied_content, to_path}``. Unlike ``knowledge_suggestions.to_path`` (0002), none of these was
ever added to an existing table by the additive reconcile — every one has been part of its
table's ``create_table`` since the table's first release, so the column has been ``NOT NULL`` at
the database level from day one and cannot hold a ``NULL`` row: there is no reconcile-shaped
history here for #903 to have produced.

Nothing to repair, then — every insert already sets these columns (the ORM applies the Python
default before the row is written) — but the audit still calls for closing the gap between what
the model *means* and what the database *enforces*, so a future raw insert or a hand-rolled
migration cannot leave one NULL either. The backfill ``UPDATE`` below is a defensive no-op on
every database this revision will ever meet; it stays because it is what makes this revision
correct standing alone, without relying on that history holding forever.
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
    """Backfill (a no-op today) and add the server default the model now declares."""
    op.execute(
        "UPDATE knowledge_suggestions SET proposed_content = '' WHERE proposed_content IS NULL"
    )
    op.execute("UPDATE knowledge_suggestions SET origin = 'agent' WHERE origin IS NULL")
    op.execute("UPDATE knowledge_suggestions SET note = '' WHERE note IS NULL")
    with op.batch_alter_table("knowledge_suggestions") as batch_op:
        batch_op.alter_column(
            "proposed_content",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("''"),
        )
        batch_op.alter_column(
            "origin",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            server_default=sa.text("'agent'"),
        )
        batch_op.alter_column(
            "note",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("''"),
        )

    op.execute("UPDATE knowledge_suggestion_decisions SET origin = 'agent' WHERE origin IS NULL")
    op.execute("UPDATE knowledge_suggestion_decisions SET note = '' WHERE note IS NULL")
    op.execute(
        "UPDATE knowledge_suggestion_decisions SET proposed_content = '' "
        "WHERE proposed_content IS NULL"
    )
    op.execute(
        "UPDATE knowledge_suggestion_decisions SET applied_content = '' "
        "WHERE applied_content IS NULL"
    )
    op.execute("UPDATE knowledge_suggestion_decisions SET to_path = '' WHERE to_path IS NULL")
    with op.batch_alter_table("knowledge_suggestion_decisions") as batch_op:
        batch_op.alter_column(
            "origin",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            server_default=sa.text("'agent'"),
        )
        batch_op.alter_column(
            "note",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("''"),
        )
        batch_op.alter_column(
            "proposed_content",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("''"),
        )
        batch_op.alter_column(
            "applied_content",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("''"),
        )
        batch_op.alter_column(
            "to_path",
            existing_type=sa.String(length=4096),
            existing_nullable=False,
            server_default=sa.text("''"),
        )


def downgrade() -> None:
    """Drop the server defaults this revision added. Never un-backfills (there was nothing to)."""
    with op.batch_alter_table("knowledge_suggestion_decisions") as batch_op:
        batch_op.alter_column(
            "to_path",
            existing_type=sa.String(length=4096),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "applied_content",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "proposed_content",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "note", existing_type=sa.Text(), existing_nullable=False, server_default=None
        )
        batch_op.alter_column(
            "origin",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            server_default=None,
        )

    with op.batch_alter_table("knowledge_suggestions") as batch_op:
        batch_op.alter_column(
            "note", existing_type=sa.Text(), existing_nullable=False, server_default=None
        )
        batch_op.alter_column(
            "origin",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "proposed_content",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=None,
        )
