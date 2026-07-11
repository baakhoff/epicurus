"""Scheduled turns — recurring prompts that run unattended and deliver into a session.

The agent is purely reactive: every turn today needs an HTTP caller or an inbound bridge
message. A scheduled turn is the time-driven half of proactivity (the event-driven half —
listeners/alerts — is a later milestone): an operator-authored prompt ("summarize today's
calendar, unread mail, and due tasks") that fires on its own, daily or weekly, at a local
hour, and lands in an ordinary chat session the operator can open like any other.

This module owns:

* the tenant-scoped ``scheduled_turns`` table + :class:`ScheduledTurnStore` (CRUD),
* :class:`ScheduledTurnScheduler`, a background poll loop that finds due rows and runs each
  as a headless :class:`~epicurus_core_app.agent.agent.Agent` turn — the same shape
  :class:`~epicurus_core_app.messaging.inbound.InboundConsumer` already uses for a bridge
  message (no HTTP caller, an explicit ``tenant_id`` + ``session_id``).

v1 is single-runner and poll-based rather than N concurrent ``sleep_until_hour`` tasks
(``scheduling.py``): each row carries its own independently configured hour (and, for a
weekly cadence, weekday), and rows are created/paused/deleted at runtime — a fixed set of
per-hour sleeps can't express that. The poll tick reuses the same timezone-resolution
pattern (a zero-arg async provider) the nightly extraction drain and the maintenance
orchestrator already wake against.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import ChatMessage, get_logger
from epicurus_core_app.scheduling import TimezoneProvider

if TYPE_CHECKING:
    from epicurus_core_app.agent.agent import Agent
    from epicurus_core_app.llm.power import PowerController

log = get_logger("epicurus_core_app.scheduled_turns")

Cadence = Literal["daily", "weekly"]
_CADENCES = frozenset({"daily", "weekly"})


@dataclass
class ScheduledTurn:
    """One recurring prompt (tenant-scoped) — an immutable value returned by the store."""

    id: str
    tenant: str
    prompt: str
    cadence: Cadence
    hour: int
    weekday: int | None  # 0=Monday..6=Sunday; set only when cadence == "weekly"
    delivery_target: str  # the chat session id the turn delivers into
    enabled: bool
    created_at: datetime
    last_run_at: datetime | None
    last_status: str | None


def validate_cadence(cadence: str, weekday: int | None) -> None:
    """Raise ``ValueError`` if *cadence*/*weekday* don't form a valid schedule."""
    if cadence not in _CADENCES:
        raise ValueError(f"cadence must be one of {sorted(_CADENCES)}, got {cadence!r}")
    if cadence == "weekly" and (weekday is None or not (0 <= weekday <= 6)):
        raise ValueError("weekday (0=Monday..6=Sunday) is required for a weekly cadence")


# ── persistence ──────────────────────────────────────────────────────────────


class _Base(DeclarativeBase):
    pass


class _StoredScheduledTurn(_Base):
    """ORM mapping for one scheduled turn (tenant-scoped).

    ``pk`` is an internal autoincrement key used only for insertion-ordered listing; ``id``
    (a uuid hex) is the opaque external key every other method keys on — mirroring the
    suggestion-queue pattern (an internal int PK, a separate opaque id) rather than making the
    uuid itself the primary key: two rows created within the same second (SQLite's
    ``created_at`` has only second resolution) would otherwise tie-break on the uuid string,
    which sorts randomly, not chronologically.
    """

    __tablename__ = "scheduled_turns"

    pk: Mapped[int] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(String(32), index=True, unique=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    cadence: Mapped[str] = mapped_column(String(16))
    hour: Mapped[int] = mapped_column(Integer)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_target: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ScheduledTurnStore:
    """CRUD for the tenant-scoped scheduled-turn rows in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def create(
        self,
        *,
        tenant: str,
        prompt: str,
        cadence: str,
        hour: int,
        weekday: int | None,
        delivery_target: str,
    ) -> ScheduledTurn:
        """Stage a new scheduled turn, enabled from the start, and return it."""
        async with self._session() as session:
            row = _StoredScheduledTurn(
                id=uuid.uuid4().hex,
                tenant=tenant,
                prompt=prompt,
                cadence=cadence,
                hour=hour % 24,
                weekday=weekday,
                delivery_target=delivery_target,
                enabled=True,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_value(row)

    async def list_enabled(self) -> list[ScheduledTurn]:
        """Every enabled row across all tenants — what the scheduler tick evaluates.

        Defined before ``list()`` below: a method literally named ``list`` shadows the
        builtin for every subsequent annotation in this class body, so any other
        ``list[...]``-returning method must come first.
        """
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredScheduledTurn).where(_StoredScheduledTurn.enabled.is_(True))
            )
            return [_to_value(row) for row in rows]

    async def list(self, *, tenant: str) -> list[ScheduledTurn]:
        """All of a tenant's scheduled turns, oldest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredScheduledTurn)
                .where(_StoredScheduledTurn.tenant == tenant)
                .order_by(_StoredScheduledTurn.pk)
            )
            return [_to_value(row) for row in rows]

    async def get(self, *, tenant: str, turn_id: str) -> ScheduledTurn | None:
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredScheduledTurn).where(
                    _StoredScheduledTurn.tenant == tenant, _StoredScheduledTurn.id == turn_id
                )
            )
            return _to_value(row) if row is not None else None

    async def set_enabled(self, *, tenant: str, turn_id: str, enabled: bool) -> bool:
        """Pause/resume a scheduled turn. True if a row was found and updated."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredScheduledTurn).where(
                    _StoredScheduledTurn.tenant == tenant, _StoredScheduledTurn.id == turn_id
                )
            )
            if row is None:
                return False
            row.enabled = enabled
            await session.commit()
            return True

    async def delete(self, *, tenant: str, turn_id: str) -> bool:
        """Remove a scheduled turn. True if a row was found and deleted."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredScheduledTurn).where(
                    _StoredScheduledTurn.tenant == tenant, _StoredScheduledTurn.id == turn_id
                )
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def mark_run(self, *, turn_id: str, status: str, ran_at: datetime) -> None:
        """Record the outcome of a run (or a paused-skip) against its row.

        Looks up by the opaque ``id`` (not ``session.get``, which keys on the primary key —
        the internal autoincrement ``pk``, not this externally-known id).
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredScheduledTurn).where(_StoredScheduledTurn.id == turn_id)
            )
            if row is None:  # deleted between being read and being run — nothing to record
                return
            row.last_run_at = ran_at
            row.last_status = status[:255]
            await session.commit()


def _to_value(row: _StoredScheduledTurn) -> ScheduledTurn:
    return ScheduledTurn(
        id=row.id,
        tenant=row.tenant,
        prompt=row.prompt,
        cadence=row.cadence,  # type: ignore[arg-type]  # validated at creation
        hour=row.hour,
        weekday=row.weekday,
        delivery_target=row.delivery_target,
        enabled=row.enabled,
        created_at=row.created_at,
        last_run_at=row.last_run_at,
        last_status=row.last_status,
    )


# ── scheduling ─────────────────────────────────────────────────────────────


class ScheduledTurnScheduler:
    """Wakes periodically, finds due rows, and runs each as a headless agent turn.

    A single poll loop, not one task per row: v1 has an operator-scale row count and one core
    instance, so a plain tick every ``poll_interval_s`` is simpler and correct — see the module
    docstring for why the existing per-hour ``sleep_until_hour`` primitive doesn't fit a
    dynamic, independently-configured-per-row schedule. Due rows run **sequentially** (gentle
    on a single local GPU, mirroring the nightly extraction drain and the maintenance batch).
    """

    def __init__(
        self,
        store: ScheduledTurnStore,
        agent: Agent,
        power: PowerController,
        *,
        timezone: TimezoneProvider,
        poll_interval_s: int = 60,
    ) -> None:
        self._store = store
        self._agent = agent
        self._power = power
        self._timezone = timezone
        self._poll_interval_s = poll_interval_s

    async def run_periodic(self) -> None:
        """Loop forever, ticking every ``poll_interval_s`` — never dies on a transient error."""
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self.tick()
            except Exception as exc:  # a bad tick must not kill the scheduler
                log.warning("scheduled-turn tick failed", error=str(exc))

    async def tick(self) -> None:
        """Evaluate every enabled row against the current local time; run the due ones."""
        tz: tzinfo
        try:
            tz = ZoneInfo((await self._timezone()).strip() or "UTC")
        except Exception:  # unknown/blank/bad tz — fall back to UTC rather than skip the tick
            tz = UTC
        local_now = datetime.now(tz)
        for row in await self._store.list_enabled():
            if _is_due(row, local_now):
                await self._run_one(row)

    async def _run_one(self, row: ScheduledTurn) -> None:
        ran_at = datetime.now(UTC)
        if self._power.paused:
            # Skip and record — never queue a burst of catch-up runs. Recording the skip (not
            # just logging it) advances last_run_at so this same window isn't re-evaluated on
            # every subsequent tick while paused; the operator sees why nothing arrived.
            await self._store.mark_run(turn_id=row.id, status="skipped (paused)", ran_at=ran_at)
            log.info("scheduled turn skipped; runtime paused", id=row.id, tenant=row.tenant)
            return
        try:
            await self._agent.run(
                [ChatMessage(role="user", content=row.prompt)],
                tenant_id=row.tenant,
                session_id=row.delivery_target,
            )
            await self._store.mark_run(turn_id=row.id, status="ok", ran_at=ran_at)
            log.info(
                "scheduled turn delivered",
                id=row.id,
                tenant=row.tenant,
                session=row.delivery_target,
            )
        except Exception as exc:  # one row's failure must never break the scheduler
            await self._store.mark_run(turn_id=row.id, status=f"error: {exc}", ran_at=ran_at)
            log.warning("scheduled turn failed", id=row.id, tenant=row.tenant, error=str(exc))


def _is_due(row: ScheduledTurn, local_now: datetime) -> bool:
    """Whether *row* should fire at *local_now* and hasn't already been handled this window.

    A row is due when the local hour matches (and, for a weekly cadence, the weekday too) and
    it hasn't already run *or been skipped* today — ``last_run_at`` (set on both a real run and
    a paused-skip) is compared by local calendar date, so a tick that lands anywhere inside the
    target hour fires exactly once, not once per poll interval.
    """
    if local_now.hour != row.hour:
        return False
    if row.cadence == "weekly" and row.weekday is not None and local_now.weekday() != row.weekday:
        return False
    if row.last_run_at is not None:
        last_local = row.last_run_at.astimezone(local_now.tzinfo)
        if last_local.date() == local_now.date():
            return False
    return True


__all__ = [
    "Cadence",
    "ScheduledTurn",
    "ScheduledTurnScheduler",
    "ScheduledTurnStore",
    "validate_cadence",
]
