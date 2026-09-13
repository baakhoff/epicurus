"""normalise the knowledge_suggestions to_path default

Revision 0002, following 0001.

``knowledge_suggestions.to_path`` was declared ``server_default="''"`` — a *plain string*,
which SQLAlchemy quotes as a literal, so ``create_all`` emitted ``DEFAULT ''''''``: a default
whose value is the two characters ``''``, not the empty string the comment above it claimed.
The additive reconcile pasted the same string into ``ALTER TABLE … ADD COLUMN`` as raw SQL and
emitted ``DEFAULT ''`` — the real empty string — so a database that created the table fresh and
one that gained the column through the reconcile disagreed about their own default. Nothing
noticed, because every insert sets ``to_path`` explicitly and the read side treats a falsy
value as "no destination" either way — but a plain SQL insert that omitted the column would
have produced a row whose ``to_path`` is literally two apostrophes.

The model now says ``text("''")``, which is what its comment always claimed. This revision
brings an existing database to the same place: the default, and any row the old one produced.
It is the first change to this service's schema the reconcile could never have made — it
alters an existing column rather than adding one (#834, #931).
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
    """Set the default to ``''`` and repair any row the doubled-quote default produced."""
    # Rows first: one holding the literal two-apostrophe value came from the old default and
    # reads back as "no destination" only through the read side's falsy-value fallback. Make
    # the stored value honest.
    op.execute("UPDATE knowledge_suggestions SET to_path = '' WHERE to_path = ''''''")
    # Inside a batch block, because SQLite has no ALTER COLUMN at all: batch mode rebuilds the
    # table there (copy, move the rows, swap the names) and compiles to the plain ALTER on
    # Postgres, so one revision runs on production and on `task migrate:check` alike.
    with op.batch_alter_table("knowledge_suggestions") as batch_op:
        batch_op.alter_column(
            "to_path",
            existing_type=sa.String(length=4096),
            existing_nullable=False,
            server_default=sa.text("''"),
        )


def downgrade() -> None:
    """Restore the doubled-quote default. Deliberately does not un-repair the rows."""
    with op.batch_alter_table("knowledge_suggestions") as batch_op:
        batch_op.alter_column(
            "to_path",
            existing_type=sa.String(length=4096),
            existing_nullable=False,
            server_default=sa.text("''''''"),
        )
