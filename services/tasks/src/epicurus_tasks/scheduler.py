"""Lead-time scheduler: `tasks.task_due_soon` / `tasks.task_overdue` (#664).

Tasks has no periodic background job today — this is the first one (mirroring calendar's own
new `event_starting_soon`/`event_ended` scheduler, #664: "two modules, one pattern"). A poll
loop checks, each tick, which open tasks are inside their due-soon lead window and which are
already overdue — firing each at most once via a durable marker table that survives a restart.

Unlike calendar's minute-granular lead, a task's `due` is a **date**, not an instant (ADR-0039):
"due within 1 day" must be evaluated against the *operator's local calendar day*, not a raw UTC
timestamp — the same reason the recurrence sweep in `router.py` resolves `operator_clock` rather
than using `datetime.now(UTC)` directly. This module takes that same resolved "today" string
rather than re-deriving it, so the scheduler and the recurrence sweep can never disagree about
what day it is.

No-firehose note (#664): mirrors calendar's — the first tick after a fresh start can fire once
for every task already due-soon/overdue, which is accepted as correct (there is no "backlog"
concept to suppress here, unlike mail's initial-sync rule). The fire-once marker guarantees
exactly once, not zero and not repeated.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date, timedelta

from sqlalchemy import BigInteger, String, UniqueConstraint, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import EntityRef, EventBus, emit_event, get_logger
from epicurus_core.db import ensure_columns
from epicurus_tasks.lead_time_prefs import LeadTimePrefsStore
from epicurus_tasks.models import Task
from epicurus_tasks.providers import TasksProvider

log = get_logger("epicurus_tasks.scheduler")

TASK_DUE_SOON = "tasks.task_due_soon"
TASK_OVERDUE = "tasks.task_overdue"

_MARKER_DUE_SOON = "due_soon"
_MARKER_OVERDUE = "overdue"

DEFAULT_POLL_INTERVAL_S = 300.0
"""How often the scheduler ticks. Day-granular leads don't need calendar's minute-level
polling — five minutes keeps the provider read cheap while still noticing a new day promptly."""


class _MarkerBase(DeclarativeBase):
    pass


class _FiredMarkerRow(_MarkerBase):
    """One fire-once marker: this ``(tenant, task, marker)`` has already been emitted (#664).

    ``fired_at_ns`` is a nanosecond epoch (~1.8e18) — ``BigInteger``, never ``Integer`` (the
    knowledge-module mtime bug: SQLite tolerates the int32 overflow so unit tests pass, then
    Postgres doesn't).
    """

    __tablename__ = "tasks_fired_markers"
    __table_args__ = (
        UniqueConstraint("tenant", "task_id", "marker", name="uq_tasks_fired_marker"),
    )

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    task_id: Mapped[str] = mapped_column(String(255), index=True)
    marker: Mapped[str] = mapped_column(String(32))
    fired_at_ns: Mapped[int] = mapped_column(BigInteger)


class FiredMarkerStore:
    """Durable fire-once markers for the lead-time scheduler (#664)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_MarkerBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        ensure_columns(sync_conn, _FiredMarkerRow.__table__, ())

    async def try_claim(self, *, tenant: str, task_id: str, marker: str) -> bool:
        """Atomically claim ``(tenant, task_id, marker)`` — ``True`` if this call won.

        A database constraint decides, not a read-then-write check that races (the same
        posture the event spine's own dedup takes, ADR-0103, and calendar's own marker store).
        """
        async with self._session() as session:
            session.add(
                _FiredMarkerRow(
                    tenant=tenant, task_id=task_id, marker=marker, fired_at_ns=time.time_ns()
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def has_fired(self, *, tenant: str, task_id: str, marker: str) -> bool:
        """Whether ``(tenant, task_id, marker)`` has already been claimed — read-only,
        for tests."""
        async with self._session() as session:
            row = await session.scalar(
                select(_FiredMarkerRow.pk).where(
                    _FiredMarkerRow.tenant == tenant,
                    _FiredMarkerRow.task_id == task_id,
                    _FiredMarkerRow.marker == marker,
                )
            )
            return row is not None


def _task_account(task: Task) -> str:
    """local when unstamped/local (``list_id`` is ``None``, per ``TasksRouter._stamp``);
    otherwise the only external provider today, google. A real per-account field on ``Task``
    would be more precise if tasks ever gains a second external provider — today's inference
    matches the router's own local/external distinction exactly (see ``_stamp``)."""
    return "local" if task.list_id is None else "google"


def _marker_key(task: Task) -> str:
    """A fire-once marker's task identity: provider-qualified, like the emitted ``dedup_key``."""
    return f"{_task_account(task)}:{task.id}"


async def _fire_once(
    *,
    markers: FiredMarkerStore,
    bus: EventBus,
    tenant: str,
    task: Task,
    event_type: str,
    marker: str,
    extra_payload: dict[str, object] | None = None,
) -> None:
    key = _marker_key(task)
    if not await markers.try_claim(tenant=tenant, task_id=key, marker=marker):
        return
    payload: dict[str, object] = {"title": task.title[:200], "due": task.due}
    if extra_payload:
        payload.update(extra_payload)
    try:
        await emit_event(
            bus,
            tenant_id=tenant,
            module="tasks",
            event_type=event_type,
            dedup_key=f"{key}:{marker}",
            payload=payload,
            entity_ref=EntityRef(ref_id=task.id, module="tasks", kind="task", title=task.title),
        )
    except Exception as exc:  # a spine hiccup must never crash the scheduler tick
        log.warning(f"{event_type} emit failed", task_id=task.id, error=str(exc))


async def tick(
    *,
    tenant: str,
    provider: TasksProvider,
    lead_prefs: LeadTimePrefsStore,
    markers: FiredMarkerStore,
    bus: EventBus,
    today: str,
) -> None:
    """One scheduler pass: fire `task_due_soon` / `task_overdue` for anything newly due.

    *today* is the operator's local calendar date (ISO, resolved by the caller via the same
    `operator_clock` the recurrence sweep uses) — not `datetime.now(UTC)`, so a task due
    "tomorrow" in the operator's timezone is never flagged a day early or late (ADR-0039).
    *provider* is typed as the `TasksProvider` Protocol (not the concrete `TasksRouter`) —
    all this scheduler needs is `list_tasks`, and the looser type matches how `app.py` already
    holds the router.
    """
    lead_days = await lead_prefs.get_lead_days(tenant)
    now_date = date.fromisoformat(today)
    due_soon_cutoff = now_date + timedelta(days=lead_days)
    tasks = await provider.list_tasks(tenant, scope="open")
    for task in tasks:
        if not task.due:
            continue
        try:
            task_date = date.fromisoformat(task.due[:10])
        except ValueError:
            log.warning(
                "tasks scheduler: unparseable due date; skipping", task_id=task.id, due=task.due
            )
            continue
        if task_date < now_date:
            await _fire_once(
                markers=markers,
                bus=bus,
                tenant=tenant,
                task=task,
                event_type=TASK_OVERDUE,
                marker=_MARKER_OVERDUE,
            )
        elif now_date <= task_date <= due_soon_cutoff:
            await _fire_once(
                markers=markers,
                bus=bus,
                tenant=tenant,
                task=task,
                event_type=TASK_DUE_SOON,
                marker=_MARKER_DUE_SOON,
                extra_payload={"lead_days": lead_days},
            )


async def run_periodic(
    *,
    tenant: str,
    provider: TasksProvider,
    lead_prefs: LeadTimePrefsStore,
    markers: FiredMarkerStore,
    bus: EventBus,
    today: Callable[[], Awaitable[str]],
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Poll forever, ticking first and sleeping after — so a fresh restart checks promptly
    rather than waiting a full interval before its first pass. One bad tick (a provider hiccup,
    a transient DB error) is logged and skipped, never kills the loop."""
    while True:
        try:
            await tick(
                tenant=tenant,
                provider=provider,
                lead_prefs=lead_prefs,
                markers=markers,
                bus=bus,
                today=await today(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("tasks lead-time scheduler tick failed", error=str(exc))
        await asyncio.sleep(poll_interval_s)
