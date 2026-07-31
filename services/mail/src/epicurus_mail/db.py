"""Tenant-scoped local mail cache (ADR-0096, #623).

The mail module was stateless: every mailbox open fanned out ~28 Gmail calls (labels +
one metadata fetch per thread), so opening Mail was slow. This store mirrors just enough
to render the landing view instantly and lets a background reconcile pull only the delta:

- ``mail_thread`` — the materialized landing rows for a ``(tenant, label)``, ordered by
  ``sort_ts`` (the thread's last-message epoch **milliseconds**, a ``BigInteger``).
- ``mail_label`` — the rail's folders + unread counts, cached per tenant.
- ``mail_category`` — the Inbox's category tabs (Primary / Promotions / …) with their unread
  counts and newest-message previews, per ``(tenant, label)`` and TTL'd (#765). Assembling
  them is a provider fan-out, so it is cached rather than repeated per render.
- ``mail_landing`` — per ``(tenant, label)`` landing metadata: the page-1 ``next_cursor``
  (so a cached view can still offer "Older") and when it was last full-synced.
- ``mail_sync`` — the per-tenant change cursor (Gmail ``history_id``; IMAP
  ``uid_validity`` / ``uid_next`` reserved) — all ``BigInteger`` (see the module insight).

Everything is scoped by ``tenant_id`` (constraint #1) even though v1 is single-tenant. The
store owns no provider specifics — the orchestrator (:mod:`epicurus_mail.cache`) drives it
from the neutral provider seam, so an IMAP backend reuses the same schema.

There is no migration framework; like every epicurus store it evolves via
``create_all`` + the shared additive :func:`epicurus_core.db.ensure_columns` reconcile,
called from :meth:`MailCache.init` (ADR-0067). This is the tables' first release, so the
reconciled-column lists are empty today — they exist so a *later* column lands in an
already-provisioned database instead of 500ing every read.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns
from epicurus_mail.provider import (
    MailCategory,
    MailCategoryPreview,
    MailCursor,
    MailLabel,
    MailThreadSummary,
)

# The cache never grows past this many rows per (tenant, label): an incremental reconcile
# keeps bumping changed threads to the top, so old rows would otherwise accumulate. A landing
# page shows ~25; keeping 200 leaves ample headroom for "Older" jumps to still hit cache-ish
# without unbounded growth.
LANDING_KEEP = 200

# Columns added after each table's first release, reconciled in place at startup. Empty on
# first release; append a column name here when you add one to a model (ADR-0067).
_THREAD_ADDED: tuple[str, ...] = ()
_LABEL_ADDED: tuple[str, ...] = ()
_SYNC_ADDED: tuple[str, ...] = ()
_LANDING_ADDED: tuple[str, ...] = ()
_CATEGORY_ADDED: tuple[str, ...] = ()

# The ``category_id`` of the **negative-cache** row (#765): "we asked the provider and it has
# no categories here". A real category id is never empty, so this can't collide with one — and
# without it an uncategorized mailbox would re-run the provider probe on every single render,
# which is precisely the fan-out the cache exists to stop.
NO_CATEGORIES_MARKER = ""


class _Base(DeclarativeBase):
    pass


class _StoredThread(_Base):
    """One cached landing row for a ``(tenant, label)`` (ADR-0096)."""

    __tablename__ = "mail_thread"
    __table_args__ = (UniqueConstraint("tenant_id", "label", "thread_id", name="uq_mail_thread"),)

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(63), index=True)
    # The rail folder/label this row is filed under (Gmail label id, e.g. ``INBOX``).
    label: Mapped[str] = mapped_column(String(255), index=True)
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(String(1024), default="")
    sender: Mapped[str] = mapped_column(String(512), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    # The provider's raw date header, shown as-is in the row (never parsed for ordering).
    date: Mapped[str] = mapped_column(String(128), default="")
    # Ordering key: the thread's last-message epoch **milliseconds** (~1.75e12 today) — must be
    # BigInteger, not Integer (int32 overflows on Postgres; SQLite hides it in tests).
    sort_ts: Mapped[int] = mapped_column(BigInteger, default=0)
    unread: Mapped[bool] = mapped_column(Boolean, default=False)
    message_count: Mapped[int] = mapped_column(Integer, default=1)
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _StoredLabel(_Base):
    """One cached rail label for a tenant (ADR-0096)."""

    __tablename__ = "mail_label"
    __table_args__ = (UniqueConstraint("tenant_id", "label_id", name="uq_mail_label"),)

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(63), index=True)
    label_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255), default="")
    kind: Mapped[str] = mapped_column(String(16), default="system")
    # The label's unread count when the provider supplied it cheaply, else NULL (the rail
    # shows a count only when present — a capability gate, not a forced zero).
    unread: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Rail order (system labels first, then the operator's own), preserved from the provider.
    position: Mapped[int] = mapped_column(Integer, default=0)


class _StoredSync(_Base):
    """The per-tenant change cursor (ADR-0096) — all large ints are BigInteger."""

    __tablename__ = "mail_sync"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_mail_sync"),)

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(63), index=True)
    # Gmail ``historyId`` (~1e10+, climbing) — BigInteger, never Integer.
    history_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # IMAP cursor (reserved for the future IMAP provider): a folder's ``UIDVALIDITY`` and the
    # highest ``UIDNEXT`` seen. BigInteger to be safe (both can exceed int32).
    uid_validity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uid_next: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _StoredLanding(_Base):
    """Per ``(tenant, label)`` landing metadata (ADR-0096): the "Older" token + freshness."""

    __tablename__ = "mail_landing"
    __table_args__ = (UniqueConstraint("tenant_id", "label", name="uq_mail_landing"),)

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(63), index=True)
    label: Mapped[str] = mapped_column(String(255))
    # The provider's page-1 next-page token, so a cache-served landing can still offer
    # "Older" (which then pages live from page 2). NULL when the folder has no older page.
    next_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _StoredCategory(_Base):
    """One cached Inbox category tab for a ``(tenant, label)`` (#765).

    Rows are written as a set and read as a set: their shared ``cached_at`` is the TTL clock,
    and a row whose ``category_id`` is :data:`NO_CATEGORIES_MARKER` records "no categories
    here" so an uncategorized mailbox is cached too.
    """

    __tablename__ = "mail_category"
    __table_args__ = (
        UniqueConstraint("tenant_id", "label", "category_id", name="uq_mail_category"),
    )

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(63), index=True)
    # The folder the tabs sit over (the Inbox today) — tabs are scoped to one folder, so a
    # future "categories over some other label" needs no schema change.
    label: Mapped[str] = mapped_column(String(255), index=True)
    # The neutral tab id ("primary", "promotions", …) — never provider query syntax.
    category_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255), default="")
    # NULL when the provider couldn't count cheaply — the tab then shows no badge (ADR-0030).
    unread: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preview_sender: Mapped[str] = mapped_column(String(512), default="")
    preview_subject: Mapped[str] = mapped_column(String(1024), default="")
    # Distinguishes "no preview at all" (an empty category) from "a preview with blank
    # fields" (a real message with no From/Subject) — two different things to the shell.
    has_preview: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Written explicitly (not a server default) so the TTL comparison reads the same value on
    # SQLite and Postgres; SQLite hands back a naive datetime, normalized to UTC on read.
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _row_to_summary(row: _StoredThread) -> MailThreadSummary:
    """A cached row back into the neutral list model the page renders."""
    return MailThreadSummary(
        id=row.thread_id,
        subject=row.subject,
        sender=row.sender,
        snippet=row.snippet,
        date=row.date,
        unread=row.unread,
        message_count=row.message_count,
        sort_ts=row.sort_ts,
    )


def _label_to_model(row: _StoredLabel) -> MailLabel:
    return MailLabel(id=row.label_id, title=row.title, kind=row.kind, unread=row.unread)


def _category_to_model(row: _StoredCategory) -> MailCategory:
    return MailCategory(
        id=row.category_id,
        title=row.title,
        unread=row.unread,
        preview=(
            MailCategoryPreview(sender=row.preview_sender, subject=row.preview_subject)
            if row.has_preview
            else None
        ),
    )


def _as_utc(stamp: datetime) -> datetime:
    """Read a stored timestamp back as tz-aware UTC.

    SQLite has no timezone type, so SQLAlchemy hands back a **naive** datetime there while
    Postgres returns an aware one. Comparing a naive value against ``datetime.now(UTC)``
    raises ``TypeError``, so a TTL that passes on Postgres would blow up on the SQLite test
    fixture (or vice versa) — normalize once, here.
    """
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


class MailCache:
    """CRUD for the tenant-scoped mail cache (ADR-0096, #623).

    Pure persistence — no provider knowledge. The orchestrator (:mod:`epicurus_mail.cache`)
    decides *what* to write; this decides *how*. Every method is tenant-scoped.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema, then reconcile any columns added after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Additive reconcile for each cache table (ADR-0067) — no-op on first release."""
        ensure_columns(sync_conn, _StoredThread.__table__, _THREAD_ADDED)
        ensure_columns(sync_conn, _StoredLabel.__table__, _LABEL_ADDED)
        ensure_columns(sync_conn, _StoredSync.__table__, _SYNC_ADDED)
        ensure_columns(sync_conn, _StoredLanding.__table__, _LANDING_ADDED)
        ensure_columns(sync_conn, _StoredCategory.__table__, _CATEGORY_ADDED)

    # ── landing rows ─────────────────────────────────────────────────────────

    async def has_landing(self, *, tenant_id: str, label: str) -> bool:
        """Whether any cached rows exist for ``(tenant, label)`` — the cache-hit test."""
        async with self._session() as session:
            hit = await session.scalar(
                select(_StoredThread.pk)
                .where(_StoredThread.tenant_id == tenant_id, _StoredThread.label == label)
                .limit(1)
            )
            return hit is not None

    async def get_landing(
        self, *, tenant_id: str, label: str, limit: int
    ) -> list[MailThreadSummary]:
        """The top *limit* cached rows for ``(tenant, label)``, newest first (by ``sort_ts``)."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredThread)
                .where(_StoredThread.tenant_id == tenant_id, _StoredThread.label == label)
                .order_by(_StoredThread.sort_ts.desc(), _StoredThread.thread_id)
                .limit(limit)
            )
            return [_row_to_summary(r) for r in rows]

    async def replace_landing(
        self,
        *,
        tenant_id: str,
        label: str,
        threads: Sequence[MailThreadSummary],
        next_cursor: str | None,
    ) -> None:
        """Swap in a freshly fetched page for ``(tenant, label)`` (a full sync of that folder).

        Delete-then-insert (portable across SQLite/Postgres, no dialect upsert) and record the
        page-1 ``next_cursor`` so the cached view keeps its "Older" affordance.
        """
        async with self._session() as session:
            await session.execute(
                delete(_StoredThread).where(
                    _StoredThread.tenant_id == tenant_id, _StoredThread.label == label
                )
            )
            for summary in threads:
                session.add(_thread_row(tenant_id, label, summary))
            await self._put_landing_meta(session, tenant_id, label, next_cursor)
            await session.commit()

    async def upsert_thread_row(
        self, *, tenant_id: str, label: str, summary: MailThreadSummary
    ) -> None:
        """Insert or refresh one thread's row (an incremental delta touched it)."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredThread).where(
                    _StoredThread.tenant_id == tenant_id,
                    _StoredThread.label == label,
                    _StoredThread.thread_id == summary.id,
                )
            )
            session.add(_thread_row(tenant_id, label, summary))
            await session.commit()

    async def remove_thread_from_label(self, *, tenant_id: str, label: str, thread_id: str) -> None:
        """Drop one thread's row from one folder (it left that label — e.g. archived)."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredThread).where(
                    _StoredThread.tenant_id == tenant_id,
                    _StoredThread.label == label,
                    _StoredThread.thread_id == thread_id,
                )
            )
            await session.commit()

    async def remove_thread(self, *, tenant_id: str, thread_id: str) -> None:
        """Drop a thread from every folder (it was deleted at the provider)."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredThread).where(
                    _StoredThread.tenant_id == tenant_id,
                    _StoredThread.thread_id == thread_id,
                )
            )
            await session.commit()

    async def set_thread_unread(self, *, tenant_id: str, thread_id: str, unread: bool) -> None:
        """Flip a thread's cached ``unread`` flag across every folder it is cached in.

        The write-through half of read/unread convergence (#623/#625): our own mark-read
        updates the cache at once, so the list reflects it before the provider round-trips.
        """
        async with self._session() as session:
            await session.execute(
                update(_StoredThread)
                .where(
                    _StoredThread.tenant_id == tenant_id,
                    _StoredThread.thread_id == thread_id,
                )
                .values(unread=unread)
            )
            await session.commit()

    async def prune_landing(self, *, tenant_id: str, label: str, keep: int = LANDING_KEEP) -> None:
        """Bound cache growth: keep only the newest *keep* rows for ``(tenant, label)``."""
        async with self._session() as session:
            survivors = (
                await session.scalars(
                    select(_StoredThread.pk)
                    .where(_StoredThread.tenant_id == tenant_id, _StoredThread.label == label)
                    .order_by(_StoredThread.sort_ts.desc(), _StoredThread.thread_id)
                    .limit(keep)
                )
            ).all()
            if not survivors:
                return
            await session.execute(
                delete(_StoredThread).where(
                    _StoredThread.tenant_id == tenant_id,
                    _StoredThread.label == label,
                    _StoredThread.pk.not_in(survivors),
                )
            )
            await session.commit()

    # ── labels (rail) ────────────────────────────────────────────────────────

    async def get_labels(self, *, tenant_id: str) -> list[MailLabel]:
        """The cached rail labels for a tenant, in stored order."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredLabel)
                .where(_StoredLabel.tenant_id == tenant_id)
                .order_by(_StoredLabel.position, _StoredLabel.pk)
            )
            return [_label_to_model(r) for r in rows]

    async def replace_labels(self, *, tenant_id: str, labels: Sequence[MailLabel]) -> None:
        """Swap in a freshly fetched rail for a tenant (delete-then-insert)."""
        async with self._session() as session:
            await session.execute(delete(_StoredLabel).where(_StoredLabel.tenant_id == tenant_id))
            for position, label in enumerate(labels):
                session.add(
                    _StoredLabel(
                        tenant_id=tenant_id,
                        label_id=label.id,
                        title=label.title,
                        kind=label.kind,
                        unread=label.unread,
                        position=position,
                    )
                )
            await session.commit()

    # ── category tabs (#765) ─────────────────────────────────────────────────

    async def get_categories(
        self, *, tenant_id: str, label: str, max_age_s: float
    ) -> list[MailCategory] | None:
        """The cached tabs for ``(tenant, label)`` — tri-state (#765).

        - ``None`` — nothing cached, or the cached set is older than *max_age_s*: the caller
          must ask the provider.
        - ``[]`` — cached "this mailbox has no categories" (the negative-cache row). A real
          answer, not a miss, so an uncategorized mailbox costs one provider probe per TTL.
        - a non-empty list — the cached tabs, in their stored order.

        Freshness is taken from the **oldest** row of the set: they are written together, so
        the minimum is the set's age, and a partially-written set can never read as fresh.
        """
        async with self._session() as session:
            rows = list(
                await session.scalars(
                    select(_StoredCategory)
                    .where(_StoredCategory.tenant_id == tenant_id, _StoredCategory.label == label)
                    .order_by(_StoredCategory.position, _StoredCategory.pk)
                )
            )
            if not rows:
                return None
            oldest = min(_as_utc(row.cached_at) for row in rows)
            if (datetime.now(UTC) - oldest).total_seconds() > max_age_s:
                return None
            return [
                _category_to_model(row) for row in rows if row.category_id != NO_CATEGORIES_MARKER
            ]

    async def replace_categories(
        self, *, tenant_id: str, label: str, categories: Sequence[MailCategory]
    ) -> None:
        """Swap in a freshly assembled tab set for ``(tenant, label)`` (delete-then-insert).

        An empty *categories* writes the single :data:`NO_CATEGORIES_MARKER` row instead of
        nothing at all, so "no categories" is cached rather than re-probed every render.
        """
        now = datetime.now(UTC)
        async with self._session() as session:
            await session.execute(
                delete(_StoredCategory).where(
                    _StoredCategory.tenant_id == tenant_id, _StoredCategory.label == label
                )
            )
            if not categories:
                session.add(
                    _StoredCategory(
                        tenant_id=tenant_id,
                        label=label,
                        category_id=NO_CATEGORIES_MARKER,
                        cached_at=now,
                    )
                )
            for position, category in enumerate(categories):
                preview = category.preview
                session.add(
                    _StoredCategory(
                        tenant_id=tenant_id,
                        label=label,
                        category_id=category.id,
                        title=category.title,
                        unread=category.unread,
                        preview_sender=preview.sender if preview else "",
                        preview_subject=preview.subject if preview else "",
                        has_preview=preview is not None,
                        position=position,
                        cached_at=now,
                    )
                )
            await session.commit()

    async def invalidate_categories(self, *, tenant_id: str) -> None:
        """Drop every cached tab set for a tenant so the next read reassembles them (#765).

        Called when *we* changed something the counts depend on (a mark-read write-through) —
        the tab badge then converges on the very next page read instead of waiting out the
        TTL. Tenant-wide rather than per-label: read state is not folder-local, and the tab
        sets are tiny.
        """
        async with self._session() as session:
            await session.execute(
                delete(_StoredCategory).where(_StoredCategory.tenant_id == tenant_id)
            )
            await session.commit()

    # ── sync cursor ──────────────────────────────────────────────────────────

    async def get_cursor(self, *, tenant_id: str) -> MailCursor:
        """The tenant's stored change cursor, or an empty (cold) cursor when never synced."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredSync).where(_StoredSync.tenant_id == tenant_id)
            )
            if row is None:
                return MailCursor()
            return MailCursor(
                history_id=row.history_id,
                uid_validity=row.uid_validity,
                uid_next=row.uid_next,
            )

    async def set_cursor(self, *, tenant_id: str, cursor: MailCursor) -> None:
        """Persist the advanced change cursor (upsert, delete-then-insert)."""
        now = datetime.now(UTC)
        async with self._session() as session:
            await session.execute(delete(_StoredSync).where(_StoredSync.tenant_id == tenant_id))
            session.add(
                _StoredSync(
                    tenant_id=tenant_id,
                    history_id=cursor.history_id,
                    uid_validity=cursor.uid_validity,
                    uid_next=cursor.uid_next,
                    synced_at=now,
                )
            )
            await session.commit()

    # ── landing metadata ─────────────────────────────────────────────────────

    async def get_landing_cursor(self, *, tenant_id: str, label: str) -> str | None:
        """The cached page-1 "Older" token for ``(tenant, label)`` (``None`` if none)."""
        async with self._session() as session:
            return await session.scalar(
                select(_StoredLanding.next_cursor).where(
                    _StoredLanding.tenant_id == tenant_id, _StoredLanding.label == label
                )
            )

    @staticmethod
    async def _put_landing_meta(
        session: AsyncSession, tenant_id: str, label: str, next_cursor: str | None
    ) -> None:
        """Upsert the landing meta inside an open session (delete-then-insert)."""
        await session.execute(
            delete(_StoredLanding).where(
                _StoredLanding.tenant_id == tenant_id, _StoredLanding.label == label
            )
        )
        session.add(
            _StoredLanding(
                tenant_id=tenant_id,
                label=label,
                next_cursor=next_cursor,
                synced_at=datetime.now(UTC),
            )
        )


def _thread_row(tenant_id: str, label: str, summary: MailThreadSummary) -> _StoredThread:
    """A stored row from a neutral summary (shared by replace + upsert)."""
    return _StoredThread(
        tenant_id=tenant_id,
        label=label,
        thread_id=summary.id,
        subject=summary.subject,
        sender=summary.sender,
        snippet=summary.snippet,
        date=summary.date,
        sort_ts=summary.sort_ts,
        unread=summary.unread,
        message_count=summary.message_count,
    )
