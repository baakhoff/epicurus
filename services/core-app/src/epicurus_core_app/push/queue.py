"""Quiet-hours queue + digest scheduler.

Default posture (ADR-0102): a notification whose category/automation wants push but lands
inside the tenant's quiet-hours window is **queued and delivered as one digest push once
the window ends** — never dropped. :class:`PushDigestScheduler` is a plain poll loop (the
same shape as :class:`~epicurus_core_app.scheduled_turns.ScheduledTurnScheduler` and the
maintenance orchestrator): wake, check every tenant with queued rows, flush the ones whose
local quiet window has ended.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import get_logger
from epicurus_core_app.push.prefs import PushPrefsStore, is_quiet_now
from epicurus_core_app.scheduling import TimezoneProvider

__all__ = ["PushDigestScheduler", "PushQueueStore", "QueuedPush", "SendDigest"]

log = get_logger("epicurus_core_app.push.queue")


@dataclass(frozen=True)
class QueuedPush:
    """One notification held back by quiet hours, awaiting the end-of-window digest."""

    tenant: str
    category: str
    title: str
    body: str
    deep_link: str | None
    entity_ref: dict[str, Any] | None
    queued_at: datetime


# A tenant + its queued items -> deliver one digest push. Bound to PushService.send_digest
# at wiring time (app.py) rather than imported directly, so this module never depends on
# push/service.py (which itself depends on this module to enqueue) — a plain callback
# breaks the cycle instead of restructuring either side around it. The scheduler discards
# whatever the callable returns (see _flush), hence Awaitable[Any] rather than importing
# NotifyResult just to name it.
SendDigest = Callable[[str, list[QueuedPush]], Awaitable[Any]]


class _Base(DeclarativeBase):
    pass


class _QueuedPushRow(_Base):
    """ORM mapping for one queued (quiet-hours-held) notification."""

    __tablename__ = "push_queue"

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    category: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    deep_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded EntityRef (epicurus_core.contracts.EntityRef.model_dump()), or NULL.
    entity_ref_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PushQueueStore:
    """Holds notifications quiet hours deferred, keyed by tenant."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def enqueue(
        self,
        *,
        tenant: str,
        category: str,
        title: str,
        body: str,
        deep_link: str | None = None,
        entity_ref: dict[str, Any] | None = None,
    ) -> None:
        async with self._session() as session:
            session.add(
                _QueuedPushRow(
                    tenant=tenant,
                    category=category,
                    title=title,
                    body=body,
                    deep_link=deep_link,
                    entity_ref_json=json.dumps(entity_ref) if entity_ref is not None else None,
                )
            )
            await session.commit()

    async def distinct_tenants(self) -> list[str]:
        """Every tenant with at least one queued row — what the scheduler tick evaluates."""
        async with self._session() as session:
            result = await session.scalars(select(_QueuedPushRow.tenant).distinct())
            return list(result)

    async def list_for_tenant(self, tenant: str) -> list[QueuedPush]:
        """A tenant's queued rows, oldest first (the digest lists them in this order)."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_QueuedPushRow)
                .where(_QueuedPushRow.tenant == tenant)
                .order_by(_QueuedPushRow.pk)
            )
            return [_to_value(row) for row in rows]

    async def delete_for_tenant(self, tenant: str) -> int:
        """Clear every queued row for *tenant* (after its digest was sent). Returns the count."""
        async with self._session() as session:
            rows = list(
                await session.scalars(select(_QueuedPushRow).where(_QueuedPushRow.tenant == tenant))
            )
            for row in rows:
                await session.delete(row)
            await session.commit()
            return len(rows)


def _to_value(row: _QueuedPushRow) -> QueuedPush:
    return QueuedPush(
        tenant=row.tenant,
        category=row.category,
        title=row.title,
        body=row.body,
        deep_link=row.deep_link,
        entity_ref=json.loads(row.entity_ref_json) if row.entity_ref_json else None,
        queued_at=row.queued_at,
    )


class PushDigestScheduler:
    """Wakes periodically; flushes any tenant whose quiet-hours window has just ended."""

    def __init__(
        self,
        queue: PushQueueStore,
        prefs: PushPrefsStore,
        send_digest: SendDigest,
        *,
        timezone: TimezoneProvider,
        poll_interval_s: int = 60,
    ) -> None:
        self._queue = queue
        self._prefs = prefs
        self._send_digest = send_digest
        self._timezone = timezone
        self._poll_interval_s = poll_interval_s

    async def run_periodic(self) -> None:
        """Loop forever, ticking every ``poll_interval_s`` — never dies on a transient error."""
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self.tick()
            except Exception as exc:  # a bad tick must not kill the scheduler
                log.warning("push digest tick failed", error=str(exc))

    async def tick(self) -> None:
        """Flush every tenant with queued rows whose quiet window is no longer active.

        Local time is resolved once per tick, not per tenant — the same single
        operator-configured timezone every other scheduler in this service reads (see
        ``scheduled_turns.py``/``maintenance.py``); true per-tenant timezones are a
        multi-tenant follow-up, not something this loop can express on its own.
        """
        local_now = await self._local_now()
        for tenant in await self._queue.distinct_tenants():
            prefs = await self._prefs.get(tenant)
            if is_quiet_now(prefs, local_now.time()):
                continue  # still quiet — leave it queued for a later tick
            await self._flush(tenant)

    async def _flush(self, tenant: str) -> None:
        items = await self._queue.list_for_tenant(tenant)
        if not items:
            return
        try:
            await self._send_digest(tenant, items)
        except Exception as exc:  # a send failure must not lose the tenant's queue silently
            log.warning(
                "push digest send failed; leaving items queued", tenant=tenant, error=str(exc)
            )
            return
        await self._queue.delete_for_tenant(tenant)

    async def _local_now(self) -> datetime:
        tz: tzinfo
        try:
            tz = ZoneInfo((await self._timezone()).strip() or "UTC")
        except Exception:  # unknown/blank/bad tz — fall back to UTC rather than skip the tick
            tz = UTC
        return datetime.now(tz)
