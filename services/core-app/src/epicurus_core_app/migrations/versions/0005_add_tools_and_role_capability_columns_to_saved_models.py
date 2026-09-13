"""add tools and role capability columns to saved_models

Revision 0005, following 0004.

Three nullable columns for the extended capability record (#944, #947, ADR-0140):
``tools_override`` and ``role_override`` (the operator's corrections, same vocabulary as the
existing ``vision_override``) and ``tools_learned`` (what the gateway learned from a provider
that refused a tool list). All nullable with no server default, so there is nothing to backfill
— NULL *is* the "auto" case, exactly as it is for ``vision_override``.

**Why the guard, when ADR-0138 says a revision after the baseline is ordinary Alembic.** The
`migrations` gate's *adoption* arm builds a database with ``create_all`` from the **current**
models — which already carry these columns — and then runs ``upgrade head`` over it. A bare
``op.add_column`` is correct on a real deployment (whose ``saved_models`` predates this PR) and
a duplicate-column failure on that arm. Adding the column only when it is absent is right in
both, and on a fresh install the regenerated baseline creates it and this revision finds
nothing to do. This is the first column added to core-app after adoption, so it is also the
first time the case arises; the rule is written up in `docs/developer/migrations.md`.
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


def _present() -> set[str]:
    """The column names ``saved_models`` already carries."""
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    """Add the tools / role capability columns that are not already there."""
    present = _present()
    for name, length in _COLUMNS:
        if name not in present:
            op.add_column(_TABLE, sa.Column(name, sa.String(length=length), nullable=True))


def downgrade() -> None:
    """Drop them again (the capability record reverts to vision + context length)."""
    present = _present()
    for name, _ in reversed(_COLUMNS):
        if name in present:
            op.drop_column(_TABLE, name)
