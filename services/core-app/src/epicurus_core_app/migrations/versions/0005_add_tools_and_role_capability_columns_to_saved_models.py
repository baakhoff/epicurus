"""add tools and role capability columns to saved_models

Revision 0005, following 0004.

Three nullable columns for the extended capability record (#944, #947, ADR-0140):
``tools_override`` and ``role_override`` (the operator's corrections, same vocabulary as the
existing ``vision_override``) and ``tools_learned`` (what the gateway learned from a provider
that refused a tool list). All nullable with no server default, so there is nothing to backfill
— NULL *is* the "auto" case, exactly as it is for ``vision_override``.

Ordinary Alembic, which is what ADR-0138 says a revision after the baseline is: the baseline
describes the schema as it stood the day core-app adopted Alembic, and every state after that is
known exactly, so ``op.add_column`` needs no guard — one would only hide a real disagreement
between the revisions and the database. The adoption arms of the gates build their "pre-Alembic"
database by running the baseline rather than ``create_all``, for the same reason
(`docs/developer/migrations.md`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "saved_models"
# (name, length) — built fresh per call: ``op.add_column`` attaches the Column to a Table, so a
# module-level instance could not be used twice.
_COLUMNS = (("tools_override", 8), ("role_override", 16), ("tools_learned", 8))


def upgrade() -> None:
    """Add the tools / role capability columns."""
    for name, length in _COLUMNS:
        op.add_column(_TABLE, sa.Column(name, sa.String(length=length), nullable=True))


def downgrade() -> None:
    """Drop them again (the capability record reverts to vision + context length)."""
    for name, _ in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
