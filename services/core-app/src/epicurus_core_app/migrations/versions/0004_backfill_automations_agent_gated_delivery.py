"""backfill automations agent_gated_delivery

Revision 0004, following 0003.

The second — and, on this service's audit, last — column in the same position as
``module_prefs.suggestions_enabled`` (revision 0003). ``automations`` shipped with #682 on
2026-07-22; ``agent_gated_delivery`` joined it four days later with #706, ``NOT NULL`` in the
model and with no server default, so the additive reconcile added it **nullable** on every
deployment already running and left existing automations with ``NULL``. ``_to_value`` coerces
that to ``False`` on read, which is why nothing visibly broke — but the column is not what the
model declares, and only a migration can close that.

The model now also declares ``server_default=text("false")``, so a freshly created table and one
the baseline reconciles end up identical and ``alembic check`` can hold them to it.

Idempotent and harmless on a deployment that never had the ``NULL``s: the ``UPDATE`` matches no
rows and the ``ALTER`` restates what is already true.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Give the reconciled ``NULL`` rows ``False`` — what readers already saw — then enforce it."""
    # The UPDATE first, so SET NOT NULL has nothing left to refuse.
    op.execute(
        "UPDATE automations SET agent_gated_delivery = false WHERE agent_gated_delivery IS NULL"
    )
    with op.batch_alter_table("automations") as batch_op:
        batch_op.alter_column(
            "agent_gated_delivery",
            existing_type=sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        )


def downgrade() -> None:
    """Let the column be ``NULL`` again, and drop the server default (the rows keep ``False``)."""
    with op.batch_alter_table("automations") as batch_op:
        batch_op.alter_column(
            "agent_gated_delivery",
            existing_type=sa.Boolean(),
            server_default=None,
            nullable=True,
        )
