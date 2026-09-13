"""normalise the literal server defaults

Revision 0002, following 0001.

Five columns were declared with a *plain string* ``server_default`` — ``"'{}'"``, ``"'[]'"``,
``"0"`` — which SQLAlchemy quotes as a literal. So ``create_all`` emitted ``DEFAULT '''{}'''``:
a default whose value is the four characters ``'{}'``, quotes included. The additive reconcile
pasted the same string into ``ALTER TABLE … ADD COLUMN`` as raw SQL and emitted ``DEFAULT '{}'``,
so a database that created the table fresh and one that gained the column through the reconcile
disagreed about their own default. Nothing noticed, because every insert sets these columns
explicitly and the row-readers coerce anything unexpected back — ``json.loads(row.x or "[]")``
treats ``"'[]'"`` as unparseable and the caller falls back — but a plain SQL insert that omitted
the column wrote a JSON document with quotes wrapped around it.

The models now say ``text("'{}'")``, which is what their comments always claimed. This revision
brings an existing database to the same place: the defaults, and any row the old ones produced.
It alters existing columns rather than adding any, so it is a change the additive reconcile
could never have made (#834, #927; ``storage``'s 0002 is the same fix, one column).

Each ``alter_column`` runs inside ``op.batch_alter_table`` because SQLite has no ``ALTER COLUMN``
at all: batch mode rebuilds the table there (copy, move the rows, swap the names) and compiles
to the plain ``ALTER`` on Postgres, so one revision runs on production and under
``task migrate:check`` alike.
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
    """Repair the rows the doubled-quote defaults produced, then fix the defaults."""
    # Rows first. A value holding the quoted form came from the old default and reads back as
    # the intended one only through a row-reader's fallback; make what is stored honest.
    op.execute("UPDATE llm_prefs SET hidden_models = '[]' WHERE hidden_models = '''[]'''")
    op.execute("UPDATE module_prefs SET models = '{}' WHERE models = '''{}'''")
    op.execute("UPDATE module_prefs SET disabled_tools = '[]' WHERE disabled_tools = '''[]'''")
    op.execute("UPDATE module_prefs SET collections = '{}' WHERE collections = '''{}'''")
    # `saved_models.added_at` needs no row repair: its bad default, `DEFAULT '0'` on a BIGINT,
    # still *stored* 0 — Postgres casts the quoted literal — so only the default text was wrong.

    with op.batch_alter_table("llm_prefs") as batch_op:
        batch_op.alter_column(
            "hidden_models",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'[]'"),
        )
    with op.batch_alter_table("module_prefs") as batch_op:
        batch_op.alter_column(
            "models",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'{}'"),
        )
        batch_op.alter_column(
            "disabled_tools",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'[]'"),
        )
        batch_op.alter_column(
            "collections",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'{}'"),
        )
    with op.batch_alter_table("saved_models") as batch_op:
        batch_op.alter_column(
            "added_at",
            existing_type=sa.BigInteger(),
            existing_nullable=False,
            server_default=sa.text("0"),
        )


def downgrade() -> None:
    """Restore the doubled-quote defaults. Deliberately does not un-repair the rows."""
    with op.batch_alter_table("llm_prefs") as batch_op:
        batch_op.alter_column(
            "hidden_models",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'''[]'''"),
        )
    with op.batch_alter_table("module_prefs") as batch_op:
        batch_op.alter_column(
            "models",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'''{}'''"),
        )
        batch_op.alter_column(
            "disabled_tools",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'''[]'''"),
        )
        batch_op.alter_column(
            "collections",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'''{}'''"),
        )
    with op.batch_alter_table("saved_models") as batch_op:
        batch_op.alter_column(
            "added_at",
            existing_type=sa.BigInteger(),
            existing_nullable=False,
            server_default=sa.text("'0'"),
        )
