"""Durable intake for the module event spine — the core's copy of record.

Modules announce world changes on the bus (:mod:`epicurus_core.module_events`); this is
the thing that listens. It owns three pieces that are separable on purpose:

* :class:`EventLogStore` — the tenant-scoped ``module_events`` table. Append, read back,
  prune. Knows nothing about NATS.
* :class:`EventIntake` — one cross-tenant durable consumer that parses each message, stores
  it, acks it, and fans it out live. Knows nothing about HTTP.
* the feed — :meth:`EventIntake.stream`, which replays recent history then trickles live
  events, for the observability console's Events tab (ADR-0031's second surface).

## Why the core keeps its own copy

"What happened" is a question you ask Postgres, not the bus. JetStream now holds the events
for a week (see below), but that is a delivery buffer, not an archive: it is bounded by
size, it discards its oldest under pressure, and it cannot be queried by tenant, module, or
time the way the feed, the automations matcher, and the runs ledger all need. The table is
also what makes a run auditable (it points at the exact rows that triggered it) and what
lets the feed survive a page reload.

## Delivery — at-least-once, and the ack is the contract

The spine is a JetStream stream (``*.events.>``) consumed through a **durable pull
consumer**. The rule that makes it worth anything is one line of ordering: **the message is
acked only after the row is committed.** So:

* A core that dies mid-message never acked it — JetStream redelivers it after ``ack_wait``.
* A core that restarts binds the *same* durable and resumes at its own cursor. Events
  emitted while it was down are still in the stream, and they arrive on the way back up.
* A store that fails (database down) is **naked**, not acked: the event comes back rather
  than being logged-and-lost. Redelivery is unlimited, deliberately — a delivery cap would
  turn a long outage into permanent silent loss.
* A message that can *never* be stored — malformed JSON, a failed envelope validator, a
  tenant mismatch — is **terminated**, not naked. It will not become valid on the tenth
  attempt, and an unlimited-redelivery consumer with no term is an infinite loop.

Fan-out to :meth:`EventIntake.on_event` listeners happens *after* the ack, and that is on
purpose. A slow listener holding the ack would eventually blow through ``ack_wait`` and
trigger a redelivery — which dedup would then absorb as a no-op, skipping the listeners
anyway. Acking at the durable write puts the guarantee exactly where the copy of record is.

## Dedup

Uniqueness is ``(tenant, module, dedup_key)``, enforced by a database constraint rather than
a read-then-write check, so two deliveries of the same change collapse to one row even if
they race. That constraint is what makes at-least-once delivery *safe* rather than merely
survivable: a redelivered message re-runs ``append``, the insert loses to the constraint,
and the consumer acks a no-op.

Emitters are expected to be chatty and repetitive — a poll loop re-seeing the same mail
every 60s is the *normal* case, not the error case — so the second insert losing quietly is
the designed outcome, not a failure.

Note what this does **not** do: it never updates the stored row from the duplicate. First
write wins. An event describes a change that already happened, so a later delivery of the
same change carries no newer truth.

## Tenancy

The consumer is cross-tenant (``*.events.>``) — see
:meth:`~epicurus_core.events.EventBus.pull_subscribe_any_tenant` for why the core, and only
the core, does that. Each message therefore carries two independent tenant claims: the
subject's leading token and the envelope's ``tenant_id``. They must agree. A module that
publishes tenant A's subject with tenant B's envelope is either buggy or hostile, and
either way the event is dropped rather than filed under a guess.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from nats.aio.msg import Msg
from nats.js import JetStreamContext
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
    EVENTS_ACK_WAIT_S,
    EVENTS_DURABLE,
    EVENTS_STREAM,
    EVENTS_STREAM_MAX_AGE_S,
    EVENTS_STREAM_MAX_BYTES,
    EVENTS_STREAM_SUBJECT,
    EVENTS_WILDCARD,
    EntityRef,
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

# How many messages one pull asks JetStream for, and how long it waits for them. The
# batch is a latency/throughput knob only — every message in it is stored and acked
# individually, so a larger batch never widens the at-risk window. The timeout is what
# makes the loop cancellable promptly at shutdown: it spends its idle life parked here.
_FETCH_BATCH = 32
_FETCH_TIMEOUT_S = 1.0

# Backoff after a *failed* pull (connection dropped, JetStream unavailable). Long enough
# that a NATS outage does not become a hot loop, short enough that recovery is prompt.
_FETCH_BACKOFF_S = 2.0

# Redelivery delay asked for when a store fails. The usual cause is the database being
# down or overloaded, so retrying in a second or two is pointless — give it room.
_NAK_DELAY_S = 5.0

# Boot-time retries for binding the consumer. compose starts the core on nats
# `service_started`, not on a JetStream-ready healthcheck, so a cold boot can race the
# server by a beat. Without this the race is permanent: `start()` fails once, the caller
# logs it, and the spine records nothing until someone restarts the core.
_BIND_ATTEMPTS = 3
_BIND_RETRY_S = 1.0


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
            # Deliberately no ``session.refresh(row)``. Every column is either set above or
            # carries a Python-side ``default=``, there is no ``server_default`` on this table,
            # and the sessionmaker is ``expire_on_commit=False`` — so the flushed row, ``id``
            # included, is already complete in memory. Re-reading it cost a round-trip per event
            # on the spine's hot path and, worse, could fail outright: two events arriving close
            # together run two ``append`` calls concurrently, and once their sessions interleave
            # the refresh cannot see its own row and raises "Could not refresh instance". That
            # surfaced inside the intake handler, where the bus logs and drops it (core NATS has
            # no redelivery), so the event was lost with nothing but a log line to say so.
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
    """Consumes the whole spine durably, records what arrives, and fans it out live.

    One durable consumer serves every tenant. Handlers registered via :meth:`on_event` run
    after a successful store — that is the seam the automations engine's matcher plugs
    into, and the reason it is a list of callbacks rather than a direct call: intake has no
    business knowing what consumes it.

    *ack_wait_s* is exposed for tests that need to observe a redelivery inside a test's
    lifetime; production takes the default. It is set when the durable consumer is
    *created* — a rebind to an existing durable keeps whatever it was created with, which
    is how a durable is supposed to behave.
    """

    def __init__(
        self, store: EventLogStore, bus: EventBus, *, ack_wait_s: float = EVENTS_ACK_WAIT_S
    ) -> None:
        self._store = store
        self._bus = bus
        self._ack_wait_s = ack_wait_s
        self._subscribers: list[asyncio.Queue[LoggedEvent]] = []
        self._listeners: list[Any] = []
        self._sub: JetStreamContext.PullSubscription | None = None
        self._task: asyncio.Task[None] | None = None

    def on_event(self, listener: Any) -> None:
        """Register ``async listener(LoggedEvent)``, called for each newly-stored event.

        Not called for duplicates — a consumer should act on a *change*, and a redelivery
        of a change it already saw is not one. That is also what makes at-least-once
        delivery invisible to a listener: only the delivery that actually wrote the row
        reaches it.
        """
        self._listeners.append(listener)

    async def start(self) -> None:
        """Provision the stream, bind the durable consumer, start pulling (idempotent).

        Both steps are safe to repeat on every boot: ``ensure_stream`` accepts an existing
        stream, and binding a durable that already exists *resumes* it rather than
        replaying from zero. Raises if the bind cannot be made after
        :data:`_BIND_ATTEMPTS` tries — the caller logs it, and the spine records nothing
        until the core is restarted, which is loud by design.
        """
        if self._sub is not None:
            return
        last: Exception | None = None
        for attempt in range(1, _BIND_ATTEMPTS + 1):
            try:
                await self._bind()
            except Exception as exc:
                last = exc
                log.warning(
                    "event intake could not bind the durable consumer",
                    attempt=attempt,
                    attempts=_BIND_ATTEMPTS,
                    error=str(exc),
                )
                if attempt < _BIND_ATTEMPTS:
                    await asyncio.sleep(_BIND_RETRY_S)
            else:
                return
        if last is not None:  # always true here; the loop only falls through on failure
            raise last

    async def _bind(self) -> None:
        await self._bus.ensure_stream(
            EVENTS_STREAM,
            [EVENTS_STREAM_SUBJECT],
            max_age_s=EVENTS_STREAM_MAX_AGE_S,
            max_bytes=EVENTS_STREAM_MAX_BYTES,
        )
        self._sub = await self._bus.pull_subscribe_any_tenant(
            EVENTS_WILDCARD,
            durable=EVENTS_DURABLE,
            stream=EVENTS_STREAM,
            ack_wait_s=self._ack_wait_s,
        )
        self._task = asyncio.create_task(self._pull_forever(self._sub))
        log.info(
            "event intake consuming",
            stream=EVENTS_STREAM,
            durable=EVENTS_DURABLE,
            subject=EVENTS_STREAM_SUBJECT,
        )

    async def stop(self) -> None:
        """Stop pulling and unsubscribe (best-effort) so shutdown is clean.

        A message being stored when this runs is simply never acked, so JetStream hands it
        back after ``ack_wait``. That is the correct outcome and the reason the ordering in
        :meth:`_consume` matters: losing the race costs a redelivery, never a row.
        """
        # Clear both handles *before* awaiting anything, so a second stop() (or a start()
        # racing shutdown) sees terminal state rather than a half-torn-down consumer.
        task, self._task = self._task, None
        sub, self._sub = self._sub, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if sub is not None:
            try:
                await sub.unsubscribe()
            except Exception as exc:  # draining/closed already — never fail shutdown on it
                log.warning("event intake unsubscribe failed", error=str(exc))

    async def _pull_forever(self, sub: JetStreamContext.PullSubscription) -> None:
        """Pull batches until cancelled. Never dies on a broker hiccup.

        ``CancelledError`` is a ``BaseException``, so it passes straight through the
        ``except Exception`` below and out — shutdown is not an error to be retried.
        """
        while True:
            try:
                msgs = await sub.fetch(batch=_FETCH_BATCH, timeout=_FETCH_TIMEOUT_S)
            except TimeoutError:
                # No traffic in the window. `nats.errors.TimeoutError` subclasses the
                # builtin, so this catches both it and asyncio's.
                continue
            except Exception as exc:
                log.warning("event intake pull failed; retrying", error=str(exc))
                await asyncio.sleep(_FETCH_BACKOFF_S)
                continue
            for msg in msgs:
                await self._consume(msg)

    async def _consume(self, msg: Msg) -> None:
        """Parse → verify tenancy → store → **ack** → fan out. Never raises.

        The ordering is the guarantee. Nothing is acked before the row is committed, so a
        crash anywhere above the ack returns the event to the stream. Everything below the
        ack is best-effort fan-out, which a redelivery would not re-run anyway (the
        duplicate short-circuits before the listeners).
        """
        try:
            envelope = EventEnvelope.model_validate_json(msg.data)
        except ValidationError as exc:
            # Includes a payload over the size cap or carrying a credential-shaped key:
            # the contract is enforced on the way in, not merely requested at the source.
            # Terminate rather than nak — no amount of redelivery makes it parse.
            log.warning("dropped malformed event", subject=msg.subject, error=str(exc))
            await self._terminate(msg)
            return

        subject_tenant = msg.subject.split(".", 1)[0]
        if subject_tenant != envelope.tenant_id:
            log.warning(
                "dropped event with mismatched tenant",
                subject=msg.subject,
                subject_tenant=subject_tenant,
                envelope_tenant=envelope.tenant_id,
            )
            await self._terminate(msg)
            return

        try:
            stored = await self._store.append(envelope)
        except Exception as exc:
            # The database is the copy of record and it did not take this event. Do not
            # ack: a nak sends it back so a database outage costs latency, not history.
            log.error(
                "event not recorded; asking for redelivery",
                tenant=envelope.tenant_id,
                module=envelope.module,
                type=envelope.type,
                error=str(exc),
            )
            await self._nak(msg)
            return

        # Durable now — either this delivery wrote the row, or an earlier one did and the
        # unique constraint rejected this one. Both mean "on file", so both ack.
        await self._ack(msg)

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

    async def _ack(self, msg: Msg) -> None:
        """Acknowledge, tolerating a broker that has gone away.

        A lost ack costs one redelivery, which the unique constraint absorbs — so this
        failing is a log line, never a reason to unwind the row we just committed.
        """
        try:
            await msg.ack()
        except Exception as exc:
            log.warning(
                "event ack failed; expect a redelivery", subject=msg.subject, error=str(exc)
            )

    async def _nak(self, msg: Msg) -> None:
        """Return the message to the stream after a delay."""
        try:
            await msg.nak(delay=_NAK_DELAY_S)
        except Exception as exc:
            # Not acking has the same effect once ack_wait expires, just later.
            log.warning("event nak failed", subject=msg.subject, error=str(exc))

    async def _terminate(self, msg: Msg) -> None:
        """Refuse the message permanently — it can never become valid."""
        try:
            await msg.term()
        except Exception as exc:
            log.warning("event terminate failed", subject=msg.subject, error=str(exc))

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
    ) -> AsyncGenerator[LoggedEvent, None]:
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
