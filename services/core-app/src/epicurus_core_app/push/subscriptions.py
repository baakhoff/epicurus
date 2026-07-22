"""Per-device push subscriptions (tenant-scoped) — the browser's ``PushSubscription``.

One row per subscribed device/browser. ``endpoint`` is the push service URL the browser
handed back from ``PushManager.subscribe()`` (unique per registration); ``p256dh``/``auth``
are the subscription's public key and auth secret, both required to encrypt a payload for
that device (RFC 8291). Re-subscribing the same device (same ``endpoint``) updates the row
in place rather than duplicating it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["PushSubscription", "PushSubscriptionStore"]


@dataclass(frozen=True)
class PushSubscription:
    """One subscribed device — an immutable value returned by the store."""

    id: str
    tenant: str
    endpoint: str
    p256dh: str
    auth: str
    device_label: str
    created_at: datetime
    last_seen_at: datetime | None


class _Base(DeclarativeBase):
    pass


class _StoredSubscription(_Base):
    """ORM mapping for one push subscription.

    ``pk`` is an internal autoincrement key; ``id`` (a uuid hex) is the opaque external key
    every other method keys on — the same shape as :mod:`epicurus_core_app.scheduled_turns`,
    for the same reason (stable, non-guessable, chronology-independent external ids).
    """

    __tablename__ = "push_subscriptions"
    __table_args__ = (
        UniqueConstraint("tenant", "endpoint", name="uq_push_subscriptions_tenant_endpoint"),
    )

    pk: Mapped[int] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(String(32), index=True, unique=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    endpoint: Mapped[str] = mapped_column(Text)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    device_label: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PushSubscriptionStore:
    """CRUD for the tenant-scoped push-subscription rows in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def create_or_update(
        self,
        *,
        tenant: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        device_label: str = "",
    ) -> PushSubscription:
        """Register a device, or refresh its keys/label if it's already subscribed.

        A browser re-subscribing the same registration hands back the same ``endpoint``, so
        this upserts on ``(tenant, endpoint)`` rather than creating a duplicate row.
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredSubscription).where(
                    _StoredSubscription.tenant == tenant, _StoredSubscription.endpoint == endpoint
                )
            )
            if row is None:
                row = _StoredSubscription(id=uuid.uuid4().hex, tenant=tenant, endpoint=endpoint)
                session.add(row)
            row.p256dh = p256dh
            row.auth = auth
            if device_label:
                row.device_label = device_label
            row.last_seen_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return _to_value(row)

    async def list(self, tenant: str) -> list[PushSubscription]:
        """Every device subscribed for *tenant*, oldest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredSubscription)
                .where(_StoredSubscription.tenant == tenant)
                .order_by(_StoredSubscription.pk)
            )
            return [_to_value(row) for row in rows]

    async def delete(self, *, tenant: str, sub_id: str) -> bool:
        """Remove a subscription (operator-initiated unsubscribe). True if one was found."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredSubscription).where(
                    _StoredSubscription.tenant == tenant, _StoredSubscription.id == sub_id
                )
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_by_endpoint(self, *, tenant: str, endpoint: str) -> bool:
        """Prune a subscription the push service reported Gone (404/410). True if one existed."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredSubscription).where(
                    _StoredSubscription.tenant == tenant, _StoredSubscription.endpoint == endpoint
                )
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True


def _to_value(row: _StoredSubscription) -> PushSubscription:
    return PushSubscription(
        id=row.id,
        tenant=row.tenant,
        endpoint=row.endpoint,
        p256dh=row.p256dh,
        auth=row.auth,
        device_label=row.device_label,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
    )
