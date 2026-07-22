"""The in-app notification center (#671) — the durable record of every push-worthy event,
independent of whether push itself delivered.

Written by :meth:`~epicurus_core_app.push.service.PushService.notify` whenever a
category/automation's ``center`` toggle is on (`PushPrefs`/`ChannelPrefs`, ADR-0102 §4) —
regardless of whether ``push`` also fired, was queued for quiet hours, or skipped outright.
The center is the source of truth; push is the tap on the shoulder.

Retention is a per-tenant row cap (oldest pruned past ``max_per_tenant``), not time-based —
"how many I keep" is a more predictable, storage-bounded limit than "how old" for a feed a
tenant may or may not check often.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["MAX_PER_TENANT", "Notification", "NotificationStore"]

MAX_PER_TENANT = 500


@dataclass(frozen=True)
class Notification:
    """One durable notification-center row — an immutable value returned by the store."""

    id: str
    tenant: str
    category: str
    title: str
    body: str
    deep_link: str | None
    entity_ref: dict[str, Any] | None
    automation_id: str | None
    created_at: datetime
    read_at: datetime | None


class _Base(DeclarativeBase):
    pass


class _NotificationRow(_Base):
    """ORM mapping for one notification-center row.

    ``pk`` is an internal autoincrement key (also the pruning/ordering key — insertion
    order); ``id`` (a uuid hex) is the opaque external key the routes key on — the same
    shape as :mod:`epicurus_core_app.scheduled_turns` and :mod:`epicurus_core_app.push.
    subscriptions`, for the same reason (stable, non-guessable external ids).
    """

    __tablename__ = "notifications"

    pk: Mapped[int] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(String(32), index=True, unique=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    category: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    deep_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded EntityRef (epicurus_core.contracts.EntityRef.model_dump()), or NULL.
    entity_ref_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    automation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationStore:
    """CRUD + read-state for the tenant-scoped notification-center rows in Postgres."""

    def __init__(self, engine: AsyncEngine, *, max_per_tenant: int = MAX_PER_TENANT) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)
        self._max_per_tenant = max_per_tenant

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def create(
        self,
        *,
        tenant: str,
        category: str,
        title: str,
        body: str,
        deep_link: str | None = None,
        entity_ref: dict[str, Any] | None = None,
        automation_id: str | None = None,
    ) -> Notification:
        """Record one notification, then prune the tenant back under the retention cap."""
        async with self._session() as session:
            row = _NotificationRow(
                id=uuid.uuid4().hex,
                tenant=tenant,
                category=category,
                title=title,
                body=body,
                deep_link=deep_link,
                entity_ref_json=json.dumps(entity_ref) if entity_ref is not None else None,
                automation_id=automation_id,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            value = _to_value(row)
        await self._prune(tenant)
        return value

    async def _prune(self, tenant: str) -> None:
        """Keep at most ``max_per_tenant`` rows for *tenant*, dropping the oldest first."""
        async with self._session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(_NotificationRow)
                .where(_NotificationRow.tenant == tenant)
            )
            excess = (count or 0) - self._max_per_tenant
            if excess <= 0:
                return
            stale_pks = (
                await session.scalars(
                    select(_NotificationRow.pk)
                    .where(_NotificationRow.tenant == tenant)
                    .order_by(_NotificationRow.pk)
                    .limit(excess)
                )
            ).all()
            if stale_pks:
                await session.execute(
                    delete(_NotificationRow).where(_NotificationRow.pk.in_(stale_pks))
                )
                await session.commit()

    async def list(
        self, tenant: str, *, category: str | None = None, unread_only: bool = False
    ) -> list[Notification]:
        """A tenant's notifications, newest first."""
        async with self._session() as session:
            stmt = select(_NotificationRow).where(_NotificationRow.tenant == tenant)
            if category is not None:
                stmt = stmt.where(_NotificationRow.category == category)
            if unread_only:
                stmt = stmt.where(_NotificationRow.read_at.is_(None))
            rows = await session.scalars(stmt.order_by(_NotificationRow.pk.desc()))
            return [_to_value(r) for r in rows]

    async def unread_count(self, tenant: str) -> int:
        async with self._session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(_NotificationRow)
                .where(_NotificationRow.tenant == tenant, _NotificationRow.read_at.is_(None))
            )
            return count or 0

    async def mark_read(self, *, tenant: str, notification_id: str) -> bool:
        """Mark one notification read (idempotent). True if a row was found."""
        async with self._session() as session:
            row = await session.scalar(
                select(_NotificationRow).where(
                    _NotificationRow.tenant == tenant, _NotificationRow.id == notification_id
                )
            )
            if row is None:
                return False
            if row.read_at is None:
                row.read_at = datetime.now(UTC)
                await session.commit()
            return True

    async def mark_all_read(self, tenant: str) -> int:
        """Mark every unread notification read. Returns the number marked."""
        async with self._session() as session:
            rows = list(
                await session.scalars(
                    select(_NotificationRow).where(
                        _NotificationRow.tenant == tenant, _NotificationRow.read_at.is_(None)
                    )
                )
            )
            if not rows:
                return 0
            now = datetime.now(UTC)
            for row in rows:
                row.read_at = now
            await session.commit()
            return len(rows)


def _to_value(row: _NotificationRow) -> Notification:
    return Notification(
        id=row.id,
        tenant=row.tenant,
        category=row.category,
        title=row.title,
        body=row.body,
        deep_link=row.deep_link,
        entity_ref=json.loads(row.entity_ref_json) if row.entity_ref_json else None,
        automation_id=row.automation_id,
        created_at=row.created_at,
        read_at=row.read_at,
    )
