"""add stopped to agent_messages

Revision 0006, following 0005.

Why a turn stopped, when it did not stop by answering (#944, ADR-0142). Nullable with no
backfill by design: NULL is the value every existing row should have — a turn that completed
stores nothing, so "is this reply incomplete?" is exactly "is this column set?", and rows
written before the column existed answer it correctly without being touched.

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the change."""
    op.add_column("agent_messages", sa.Column("stopped", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Reverse the change."""
    op.drop_column("agent_messages", "stopped")
