"""add auth sessions and login states

Revision 0007, following 0006.

The two tables behind sign-in with an OpenID Connect provider (#969): ``auth_sessions`` (one row
per signed-in browser, keyed by the SHA-256 of the cookie's token — never the token) and
``auth_login_states`` (one row per sign-in that has gone to the provider and not yet come back,
consumed by the callback). Both tenant-scoped, neither exported in a tenant archive.

The first post-baseline revision to add a *table*: ordinary ``op.create_table``, no guard. The
baseline describes the schema on the day core-app adopted Alembic, so every database this runs
against — fresh, adopted, or managed — reaches 0006 without these tables, and a guard would only
hide a real disagreement (ADR-0138). No server defaults, so the gate's drift arm (which drops the
columns a literal default lets the reconcile restore) has nothing to drop here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``auth_sessions`` and ``auth_login_states`` with their indexes."""
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant", sa.String(length=63), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("groups", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_auth_sessions_tenant"), "auth_sessions", ["tenant"], unique=False)
    op.create_index(
        op.f("ix_auth_sessions_expires_at"), "auth_sessions", ["expires_at"], unique=False
    )
    op.create_table(
        "auth_login_states",
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("tenant", sa.String(length=63), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("next_path", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )
    op.create_index(
        op.f("ix_auth_login_states_tenant"), "auth_login_states", ["tenant"], unique=False
    )
    op.create_index(
        op.f("ix_auth_login_states_expires_at"), "auth_login_states", ["expires_at"], unique=False
    )


def downgrade() -> None:
    """Drop both tables — every session ends and every in-flight login is lost."""
    op.drop_index(op.f("ix_auth_login_states_expires_at"), table_name="auth_login_states")
    op.drop_index(op.f("ix_auth_login_states_tenant"), table_name="auth_login_states")
    op.drop_table("auth_login_states")
    op.drop_index(op.f("ix_auth_sessions_expires_at"), table_name="auth_sessions")
    op.drop_index(op.f("ix_auth_sessions_tenant"), table_name="auth_sessions")
    op.drop_table("auth_sessions")
