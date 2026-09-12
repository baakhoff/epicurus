"""backfill the default-only not-null columns

Revision 0002, following 0001.

Seven columns are ``NOT NULL`` at the model but carry a Python-side ``default=`` and no
``server_default=`` (#834's backfill audit, #928): ``calendar_events.{all_day,excluded}``,
``calendar_sync_state.collection``, and
``calendar_synced_event.{collection,title,all_day,change_hash}``.

``calendar_events.all_day``/``excluded`` postdate that table's first release — they travel in
``LocalEventStore._ADDED_COLUMNS`` — so the additive reconcile (``epicurus_core.db.
ensure_columns``) has been adding them to any deployment provisioned before they existed, and
the reconcile's own documented limit is that a ``NOT NULL`` column with no server default
**cannot** be added to a populated table, so it added them **nullable** instead: an upgraded
deployment can genuinely hold ``NULL`` in either column today, which the row-reader coerces to
``False`` on read (#903 is the same shape of bug in another service). The three
``calendar_synced_event``/``calendar_sync_state`` columns are original, first-release columns of
tables that shipped after #831 with no reconcile history (``CalendarSyncStore._ensure_columns``
reconciles an empty column list) — every path that has ever created them, ``create_all``
included, declared them ``NOT NULL`` from the start, so there is nothing to backfill there in
practice. All seven get the same treatment regardless, so the database enforces the default
that has always been true at the application layer, instead of relying on every future writer
to keep supplying it.

``collection``, ``title`` and ``change_hash`` sit inside unique constraints
(``uq_calendar_sync_state``, ``uq_calendar_synced_event``), which is why this backfills to the
model's own default (``''``/``False``) rather than inventing a value — a column that has never
actually been nullable at the database level cannot hold two rows that collide over a shared
backfilled constant, and matching the existing Python-side default only makes explicit what
every row already has.
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
    """Backfill any NULL row to the model's default, then enforce it as a server default.

    ``nullable=False`` is passed explicitly, not just ``existing_nullable=False``: the CI
    `migrations` gate's drift arm drops each unconstrained, literal-default column
    (``_reconcilable_columns``) and lets the baseline's ``ep.create_table`` reconcile arm add it
    back — and that reconcile, working from 0001's column definition (no server default yet,
    since it predates this revision), adds it back **nullable**. Asserting only
    ``existing_nullable=False`` would have been describing a state that, on exactly that arm, is
    false — Alembic would then see nothing to change and leave the column nullable. Passing the
    target ``nullable=False`` makes this revision correct regardless of which state it finds the
    column in, matching the recipe's own worked example.
    """
    op.execute("UPDATE calendar_events SET all_day = FALSE WHERE all_day IS NULL")
    op.execute("UPDATE calendar_events SET excluded = FALSE WHERE excluded IS NULL")
    with op.batch_alter_table("calendar_events") as batch_op:
        batch_op.alter_column(
            "all_day",
            existing_type=sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        )
        batch_op.alter_column(
            "excluded",
            existing_type=sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        )

    op.execute("UPDATE calendar_sync_state SET collection = '' WHERE collection IS NULL")
    with op.batch_alter_table("calendar_sync_state") as batch_op:
        batch_op.alter_column(
            "collection",
            existing_type=sa.String(length=255),
            server_default=sa.text("''"),
            nullable=False,
        )

    op.execute("UPDATE calendar_synced_event SET collection = '' WHERE collection IS NULL")
    op.execute("UPDATE calendar_synced_event SET title = '' WHERE title IS NULL")
    op.execute("UPDATE calendar_synced_event SET all_day = FALSE WHERE all_day IS NULL")
    op.execute("UPDATE calendar_synced_event SET change_hash = '' WHERE change_hash IS NULL")
    with op.batch_alter_table("calendar_synced_event") as batch_op:
        batch_op.alter_column(
            "collection",
            existing_type=sa.String(length=255),
            server_default=sa.text("''"),
            nullable=False,
        )
        batch_op.alter_column(
            "title",
            existing_type=sa.String(length=512),
            server_default=sa.text("''"),
            nullable=False,
        )
        batch_op.alter_column(
            "all_day",
            existing_type=sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        )
        batch_op.alter_column(
            "change_hash",
            existing_type=sa.String(length=32),
            server_default=sa.text("''"),
            nullable=False,
        )


def downgrade() -> None:
    """Drop the server defaults added above. Deliberately does not un-repair any row."""
    with op.batch_alter_table("calendar_synced_event") as batch_op:
        batch_op.alter_column(
            "change_hash",
            existing_type=sa.String(length=32),
            server_default=None,
            nullable=False,
        )
        batch_op.alter_column(
            "all_day",
            existing_type=sa.Boolean(),
            server_default=None,
            nullable=False,
        )
        batch_op.alter_column(
            "title",
            existing_type=sa.String(length=512),
            server_default=None,
            nullable=False,
        )
        batch_op.alter_column(
            "collection",
            existing_type=sa.String(length=255),
            server_default=None,
            nullable=False,
        )

    with op.batch_alter_table("calendar_sync_state") as batch_op:
        batch_op.alter_column(
            "collection",
            existing_type=sa.String(length=255),
            server_default=None,
            nullable=False,
        )

    with op.batch_alter_table("calendar_events") as batch_op:
        batch_op.alter_column(
            "excluded",
            existing_type=sa.Boolean(),
            server_default=None,
            nullable=False,
        )
        batch_op.alter_column(
            "all_day",
            existing_type=sa.Boolean(),
            server_default=None,
            nullable=False,
        )
