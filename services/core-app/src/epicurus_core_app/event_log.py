"""Durable intake for the module event spine — the core's copy of record.

Modules announce world changes on the bus (:mod:`epicurus_core.module_events`); this is
the thing that listens. It owns three pieces that are separable on purpose:

* :class:`EventLogStore` — the tenant-scoped ``module_events`` table. Append, read back,
  prune. Knows nothing about NATS.
* :class:`EventIntake` — one cross-tenant subscription that parses each message, stores
  it, and fans it out live. Knows nothing about HTTP.
* the feed — :meth:`EventIntake.stream`, which replays recent history then trickles live
  events, for the observability console's Events tab (ADR-0031's second surface).

## Why the core keeps its own copy

The bus is fire-and-forget: an event published while the core is down is gone, and NATS
core holds no history to replay. So "what happened" cannot be a question you ask the bus
— it has to be a table. That table is also what makes the automations engine possible to
reason about (a run can point at the exact rows that triggered it) and what lets the feed
survive a page reload, or a restart.

## Dedup

Uniqueness is ``(tenant, module, dedup_key)``, enforced by the database rather than a
read-then-write check, so two deliveries of the same change collapse to one row even if
they race. Emitters are expected to be chatty and repetitive — a poll loop re-seeing the
same mail every 60s is the *normal* case, not the error case — so the second insert
losing quietly is the designed outcome, not a failure.

Note what this does **not** do: it never updates the stored row from the duplicate. First
write wins. An event describes a change that already happened, so a later delivery of the
same change carries no newer truth.

## Tenancy

The subscription is cross-tenant (``*.events.>``) — see
:meth:`~epicurus_core.events.EventBus.subscribe_any_tenant` for why the core, and only
the core, does that. Each message therefore carries two independent tenant claims: the
subject's leading token and the envelope's ``tenant_id``. They must agree. A module that
publishes tenant A's subject with tenant B's envelope is either buggy or hostile, and
either way the event is dropped rather than filed under a guess.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy import (
    JSON,
    CursorResult,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import (
    EVENTS_WILDCARD,
    EntityRef,
    Event,
    EventBus,
    EventEnvelope,
    get_logger,
)
from epicurus_core.redaction import redact_mapping

log = get_logger("epicurus_core_app.event_log")

# How many past events a newly-opened feed replays before going live. Sized like the log
# console's history (LogBuffer.MAX_HISTORY = 200): enough to see what just happened,
# small enough that opening the tab is one quick query.
FEED_HISTORY = 200

# Bound on the live fan-out queue per subscriber. A browser tab that stops reading must
# not grow the core's memory without limit; past this, its oldest pending events are
# dropped (the feed is a tail, not a ledger — the ledger is the table).
_SUBSCRIBER_QUEUE_MAX = 500


class LoggedEvent(BaseModel):
    """One durably-recorded event, as the feed and the API surface it.

    The envelope plus the two things only the log knows: its row ``id`` and ``received_at``
    (when the core heard it, which is *not* ``occurred_at`` — a module may report a change
    it noticed minutes late, and a digest window cares about the latter).
    """

    id: int
    tenant: str
    module: str
    type: str
    occurred_at: datetime
    received_at: datetime
    dedup_key: str
    entity_ref: EntityRef | None = None
    payload: dict[str, Any]
    schema_version: int
    # Set only on an event an automation run produced (ADR-0105). The automations matcher
    # refuses to trigger on these — the loop guard — so it must survive the round trip
    # through the log, not just the wire.
    causation_id: str | None = None


class _Base(DeclarativeBase):
    pass


class _StoredEvent(_Base):
    """ORM mapping for one recorded module event (tenant-scoped)."""

    __tablename__ = "module_events"
    __table_args__ = (
        UniqueConstraint("tenant", "module", "dedup_key", name="uq_module_events_dedup"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    module: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(128), index=True)
    # When the change happened in the world (the emitter's clock).
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # When the core recorded it (our clock) — what retention prunes on, because it is the
    # only one of the two that is guaranteed monotonic with respect to this table.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    dedup_key: Mapped[str] = mapped_column(String(255))
    entity_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    # The automations loop guard (ADR-0105): the run that produced this event, if any. A
    # module emitter always leaves it NULL — a change in the world has no cause inside the
    # system. Indexed because the matcher checks it on every single event.
    causation_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


def _to_value(row: _StoredEvent) -> LoggedEvent:
    """Read a row out as a :class:`LoggedEvent`, redacting defensively on the way.

    The envelope already refuses credential-shaped payload keys at emit, so in practice
    nothing here should need stripping. This runs anyway because it is the last point
    before the data reaches an operator's browser, and rows outlive the rule that let
    them in — a row stored under an older, laxer library version is exactly the case a
    check at the *surface* catches and a check at the *entrance* does not.
    """
    return LoggedEvent(
        id=row.id,
        tenant=row.tenant,
        module=row.module,
        type=row.type,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        dedup_key=row.dedup_key,
        entity_ref=EntityRef.model_validate(row.entity_ref) if row.entity_ref else None,
        payload=redact_mapping(row.payload or {}),
        schema_version=row.schema_version,
        causation_id=row.causation_id,
    )


class EventLogStore:
    """CRUD for the tenant-scoped ``module_events`` rows in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the table if it does not exist (idempotent).

        No ``ensure_columns`` call: this table is new in this release, so it has no
        deployed predecessor to reconcile against. The first column added *after* this
        ships must add one (ADR-0067) — ``create_all`` never alters an existing table.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def append(self, envelope: EventEnvelope) -> LoggedEvent | None:
        """Record *envelope*; returns the stored row, or ``None`` if it was a duplicate.

        Duplicate means ``(tenant, module, dedup_key)`` already exists. The database
        decides, not a prior read — so a racing second delivery is rejected by the
        constraint rather than slipping through the gap between check and insert.
        """
        async with self._session() as session:
            row = _StoredEvent(
                tenant=envelope.tenant_id,
                module=envelope.module,
                type=envelope.type,
                occurred_at=envelope.occurred_at,
                received_at=datetime.now(UTC),
                dedup_key=envelope.dedup_key,
                entity_ref=envelope.entity_ref.model_dump() if envelope.entity_ref else None,
                payload=envelope.payload,
                schema_version=envelope.schema_version,
                causation_id=envelope.causation_id,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # The unique constraint fired: this change is already on file. Roll back
                # (the session is unusable otherwise) and report the no-op to the caller.
                await session.rollback()
                return None
            await session.refresh(row)
            return _to_value(row)

    async def recent(
        self,
        *,
        tenant: str,
        limit: int = FEED_HISTORY,
        module: str | None = None,
        event_type: str | None = None,
    ) -> list[LoggedEvent]:
        """The newest events first, capped at *limit*, optionally filtered."""
        async with self._session() as session:
            stmt = (
                select(_StoredEvent)
                .where(_StoredEvent.tenant == tenant)
                .order_by(_StoredEvent.id.desc())
                .limit(limit)
            )
            if module:
                stmt = stmt.where(_StoredEvent.module == module)
            if event_type:
                stmt = stmt.where(_StoredEvent.type == event_type)
            rows = await session.scalars(stmt)
            return [_to_value(row) for row in rows]

    async def by_ids(self, *, tenant: str, ids: list[int]) -> list[LoggedEvent]:
        """The events with the given row ids (tenant-scoped; missing ids are skipped).

        The runs feed's trigger lookup (#669): a ledger entry names its triggering
        events by row id, and the feed renders their ``EntityRef`` chips. A pruned or
        foreign-tenant id simply doesn't come back — retention outliving a run's refs
        is normal, not an error.
        """
        if not ids:
            return []
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredEvent).where(_StoredEvent.tenant == tenant, _StoredEvent.id.in_(ids))
            )
            return [_to_value(row) for row in rows]

    async def prune(self, *, older_than: datetime) -> int:
        """Drop events received before *older_than*; returns how many rows went."""
        async with self._session() as session:
            result = await session.execute(
                delete(_StoredEvent).where(_StoredEvent.received_at < older_than)
            )
            await session.commit()
            return cast("CursorResult[Any]", result).rowcount or 0

    async def count(self, *, tenant: str | None = None) -> int:
        """How many events are on file (optionally for one tenant)."""
        async with self._session() as session:
            stmt = select(func.count()).select_from(_StoredEvent)
            if tenant is not None:
                stmt = stmt.where(_StoredEvent.tenant == tenant)
            return await session.scalar(stmt) or 0


class EventIntake:
    """Subscribes the whole spine, records what arrives, and fans it out live.

    One subscription serves every tenant. Handlers registered via :meth:`on_event` run
    after a successful store — that is the seam the automations engine's matcher plugs
    into (a companion issue), and the reason it is a list of callbacks rather than a
    direct call: intake has no business knowing what consumes it.
    """

    def __init__(self, store: EventLogStore, bus: EventBus) -> None:
        self._store = store
        self._bus = bus
        self._subscribers: list[asyncio.Queue[LoggedEvent]] = []
        self._listeners: list[Any] = []
        self._sub: Any = None

    def on_event(self, listener: Any) -> None:
        """Register ``async listener(LoggedEvent)``, called for each newly-stored event.

        Not called for duplicates — a consumer should act on a *change*, and a redelivery
        of a change it already saw is not one.
        """
        self._listeners.append(listener)

    async def start(self) -> None:
        """Subscribe to ``*.events.>`` (idempotent)."""
        if self._sub is not None:
            return
        self._sub = await self._bus.subscribe_any_tenant(EVENTS_WILDCARD, self._handle)
        log.info("event intake subscribed", subject=f"*.{EVENTS_WILDCARD}")

    async def stop(self) -> None:
        """Unsubscribe (best-effort) so shutdown is clean."""
        if self._sub is None:
            return
        try:
            await self._sub.unsubscribe()
        except Exception as exc:  # draining/closed already — never fail shutdown on it
            log.warning("event intake unsubscribe failed", error=str(exc))
        finally:
            self._sub = None

    async def _handle(self, event: Event) -> None:
        """Parse → verify tenancy → store → fan out. Never raises (the bus logs and drops).

        A malformed or mis-tenanted message is logged and dropped, matching how the
        inbound-messaging consumer treats a bad payload: one module's bad emit must not
        take down intake for every other module.
        """
        try:
            envelope = EventEnvelope.model_validate_json(event.data)
        except ValidationError as exc:
            # Includes a payload over the size cap or carrying a credential-shaped key:
            # the contract is enforced on the way in, not merely requested at the source.
            log.warning("dropped malformed event", subject=event.subject, error=str(exc))
            return

        subject_tenant = event.subject.split(".", 1)[0]
        if subject_tenant != envelope.tenant_id:
            log.warning(
                "dropped event with mismatched tenant",
                subject=event.subject,
                subject_tenant=subject_tenant,
                envelope_tenant=envelope.tenant_id,
            )
            return

        stored = await self._store.append(envelope)
        if stored is None:
            log.debug(
                "duplicate event ignored",
                tenant=envelope.tenant_id,
                module=envelope.module,
                dedup_key=envelope.dedup_key,
            )
            return

        log.info(
            "event recorded",
            tenant=stored.tenant,
            module=stored.module,
            type=stored.type,
            id=stored.id,
        )
        self._publish(stored)
        for listener in self._listeners:
            try:
                await listener(stored)
            except Exception as exc:  # a bad consumer must never break intake
                log.warning("event listener raised", type=stored.type, error=str(exc))

    def _publish(self, entry: LoggedEvent) -> None:
        """Hand *entry* to every live feed subscriber, dropping into a full queue."""
        for queue in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(entry)

    async def stream(
        self,
        *,
        tenant: str,
        module: str | None = None,
        event_type: str | None = None,
    ) -> AsyncIterator[LoggedEvent]:
        """Replay recent history (oldest first), then yield live events as they arrive.

        Mirrors the log console's contract (:meth:`LogBuffer.stream`) — including the
        1-second poll on the live queue, which is what lets the caller notice a closed
        browser connection promptly instead of blocking forever on an idle bus.

        The subscriber queue is registered **before** the history query so an event that
        lands mid-replay is queued rather than missed; it may then be delivered twice,
        which the caller de-duplicates on ``id``. A duplicate row in a feed is a cosmetic
        problem, a missing one is a correctness problem, and the ordering here picks the
        cosmetic side deliberately.
        """

        def _matches(entry: LoggedEvent) -> bool:
            return (
                entry.tenant == tenant
                and (not module or entry.module == module)
                and (not event_type or entry.type == event_type)
            )

        queue: asyncio.Queue[LoggedEvent] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        self._subscribers.append(queue)
        try:
            history = await self._store.recent(
                tenant=tenant, limit=FEED_HISTORY, module=module, event_type=event_type
            )
            for entry in reversed(history):  # recent() is newest-first; a feed reads oldest-first
                yield entry

            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    # Nothing pending — yield control so the caller can notice a disconnect.
                    await asyncio.sleep(0)
                    continue
                if _matches(entry):
                    yield entry
        finally:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(queue)


class EventRetention:
    """Prunes the event log on a loop, keeping a configurable window.

    Retention is time-based, not count-based: the log's job is to answer "what happened
    recently", and an operator reasons in days, not rows.
    """

    def __init__(
        self,
        store: EventLogStore,
        *,
        retention_days: int,
        interval_s: int = 3600,
    ) -> None:
        self._store = store
        self._retention_days = retention_days
        self._interval_s = interval_s

    async def run_periodic(self) -> None:
        """Loop forever, pruning every ``interval_s`` — never dies on a transient error."""
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self.prune_once()
            except Exception as exc:  # a bad prune must not kill the loop
                log.warning("event retention prune failed", error=str(exc))

    async def prune_once(self) -> int:
        """Drop everything older than the window; returns the number of rows removed."""
        if self._retention_days <= 0:  # 0/negative disables pruning — keep everything
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)
        removed = await self._store.prune(older_than=cutoff)
        if removed:
            log.info("pruned expired events", removed=removed, retention_days=self._retention_days)
        return removed


__all__ = [
    "FEED_HISTORY",
    "EventIntake",
    "EventLogStore",
    "EventRetention",
    "LoggedEvent",
]
