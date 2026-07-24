"""Persistence for the automations engine — rows, ledger, queue, kill switch.

Four tables in one module, because they share a ``Base`` and are created together:

* ``automations`` — the definitions.
* ``automation_runs`` — the ledger. **Always** written, at every autonomy level, including
  ``silent_act`` where nothing else records that anything happened.
* ``automation_queue`` — the durable work list (the ADR-0051 pattern the nightly extraction
  drain already uses): a matched trigger lands here and the runner drains it, so a restart
  mid-digest loses nothing.
* ``automation_kill_switch`` — one row per tenant. Postgres, not memory, unlike
  ``PowerController``: a safety stop that forgets itself on restart is not a safety stop.

The id convention follows ``scheduled_turns``: an internal autoincrement ``pk`` for
insertion-ordered listing plus an opaque ``id`` (uuid hex) every method keys on. Two rows
created in the same second would otherwise tie-break on a uuid string, which sorts randomly
rather than chronologically.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    JSON,
    Boolean,
    CursorResult,
    DateTime,
    Integer,
    String,
    Text,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import EntityRef, get_logger
from epicurus_core.db import ensure_columns
from epicurus_core_app.automations.model import (
    Automation,
    AutomationRun,
    AutonomyLevel,
    Cadence,
    ChatMode,
    DocumentMode,
    DocumentTarget,
    EventTrigger,
    PayloadMatcher,
    ScheduleTrigger,
    Sink,
)

log = get_logger("epicurus_core_app.automations.store")

# Columns added after these tables' first release (#682) — reconciled in place at init via the
# shared additive helper (ADR-0067), since they now have a deployed predecessor. ``sink_config``
# (#672) holds the notes/kb document targets; ``artifacts`` (#672) holds the EntityRefs a run
# produced.
_ADDED_AUTOMATION_COLUMNS = ("sink_config",)
_ADDED_RUN_COLUMNS = ("artifacts",)


class _Base(DeclarativeBase):
    pass


class _StoredAutomation(_Base):
    """ORM mapping for one automation (tenant-scoped)."""

    __tablename__ = "automations"

    pk: Mapped[int] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(String(32), index=True, unique=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(80), default="user")
    # Exactly one of these is set (validate_automation enforces it). JSON rather than a
    # column per field: a trigger is a closed vocabulary the core owns and always reads
    # whole, so flattening it would buy nothing and cost a migration per new matcher op.
    event_trigger: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    schedule_trigger: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    autonomy: Mapped[str] = mapped_column(String(20), default="notify")
    sinks: Mapped[list[str]] = mapped_column(JSON, default=list)
    chat_mode: Mapped[str] = mapped_column(String(16), default="rolling")
    chat_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rate_cap_per_hour: Mapped[int] = mapped_column(Integer, default=0)
    digest_window_minutes: Mapped[int] = mapped_column(Integer, default=0)
    # The notes/kb document targets (#672): {"notes": {path_pattern, mode}, "kb": {...}}. JSON for
    # the same reason as the trigger — a closed shape the core owns and reads whole. Nullable: it
    # was added after the table shipped (ADR-0067), so it is null for every pre-#672 row.
    sink_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(255), nullable=True)


class _StoredRun(_Base):
    """ORM mapping for one ledger entry."""

    __tablename__ = "automation_runs"

    pk: Mapped[int] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(String(32), index=True, unique=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    automation_id: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    trigger_refs: Mapped[list[int]] = mapped_column(JSON, default=list)
    filter_verdict: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Milliseconds. Integer is safe here — a duration, not an epoch. Any *epoch* column
    # would need BigInteger (see module_events / the knowledge mtime bug).
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[str] = mapped_column(Text, default="")
    sinks_fired: Mapped[list[str]] = mapped_column(JSON, default=list)
    # EntityRefs (as dicts) for documents this run produced via the notes/kb sinks (#672).
    # Nullable — added after the table shipped (ADR-0067); null/absent means none.
    artifacts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)


class _StoredQueueItem(_Base):
    """One matched trigger awaiting a run (the ADR-0051 durable-queue pattern)."""

    __tablename__ = "automation_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    automation_id: Mapped[str] = mapped_column(String(32), index=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Denormalized so a digest prompt can name what happened without re-reading the event
    # log — whose retention window is none of this queue's business.
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class _StoredKillSwitch(_Base):
    """One row per tenant: the global stop."""

    __tablename__ = "automation_kill_switch"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    halted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _StoredAutomationSession(_Base):
    """Which chat session belongs to which automation (#672) — the chat list's icon/name/grouping.

    A row exists here **only** for a session an automation with a *chat sink* wrote into (the
    runner records one when a chat-sink run persists), so a session absent from this table is an
    ordinary user chat. ``session_id`` is the primary key: a ``rolling`` automation reuses one and
    upserts the same row; a ``per_run`` automation makes a fresh session each run and they group
    under ``automation_id`` in the chat list.
    """

    __tablename__ = "automation_sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    automation_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    chat_mode: Mapped[str] = mapped_column(String(16), default="rolling")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── (de)serialization ────────────────────────────────────────────────────────


def _trigger_to_json(trigger: EventTrigger) -> dict[str, Any]:
    return {
        "module": trigger.module,
        "event_type": trigger.event_type,
        "matchers": [{"field": m.field, "op": m.op, "value": m.value} for m in trigger.matchers],
        "window_start_hour": trigger.window_start_hour,
        "window_end_hour": trigger.window_end_hour,
    }


def _trigger_from_json(data: dict[str, Any]) -> EventTrigger:
    return EventTrigger(
        module=str(data.get("module", "")),
        event_type=str(data.get("event_type", "")),
        matchers=[
            PayloadMatcher(field=m["field"], op=m["op"], value=m.get("value"))
            for m in data.get("matchers", [])
        ],
        window_start_hour=data.get("window_start_hour"),
        window_end_hour=data.get("window_end_hour"),
    )


def _schedule_to_json(trigger: ScheduleTrigger) -> dict[str, Any]:
    return {"cadence": trigger.cadence, "hour": trigger.hour, "weekday": trigger.weekday}


def _schedule_from_json(data: dict[str, Any]) -> ScheduleTrigger:
    return ScheduleTrigger(
        cadence=cast("Cadence", data.get("cadence", "daily")),
        hour=int(data.get("hour", 0)),
        weekday=data.get("weekday"),
    )


def _sink_config_to_json(
    notes: DocumentTarget | None, kb: DocumentTarget | None
) -> dict[str, Any] | None:
    """Fold the notes/kb targets into one JSON blob, or ``None`` when neither is set."""
    config: dict[str, Any] = {}
    if notes is not None:
        config["notes"] = {"path_pattern": notes.path_pattern, "mode": notes.mode}
    if kb is not None:
        config["kb"] = {"path_pattern": kb.path_pattern, "mode": kb.mode}
    return config or None


def _target_from_json(data: Any) -> DocumentTarget | None:
    if not isinstance(data, dict):
        return None
    mode = data.get("mode", "append")
    return DocumentTarget(
        path_pattern=str(data.get("path_pattern", "")),
        mode=cast("DocumentMode", mode if mode in ("create", "append") else "append"),
    )


def _to_value(row: _StoredAutomation) -> Automation:
    return Automation(
        id=row.id,
        tenant=row.tenant,
        name=row.name,
        enabled=row.enabled,
        source=row.source,
        event_trigger=_trigger_from_json(row.event_trigger) if row.event_trigger else None,
        schedule_trigger=(
            _schedule_from_json(row.schedule_trigger) if row.schedule_trigger else None
        ),
        prompt=row.prompt,
        model=row.model,
        autonomy=cast("AutonomyLevel", row.autonomy),
        sinks=[cast("Sink", s) for s in (row.sinks or [])],
        chat_mode=cast("ChatMode", row.chat_mode),
        chat_session_id=row.chat_session_id,
        rate_cap_per_hour=row.rate_cap_per_hour,
        digest_window_minutes=row.digest_window_minutes,
        created_at=row.created_at,
        last_run_at=row.last_run_at,
        last_status=row.last_status,
        notes_target=_target_from_json((row.sink_config or {}).get("notes")),
        kb_target=_target_from_json((row.sink_config or {}).get("kb")),
    )


def _run_to_value(row: _StoredRun) -> AutomationRun:
    return AutomationRun(
        id=row.id,
        tenant=row.tenant,
        automation_id=row.automation_id,
        started_at=row.started_at,
        trigger_refs=list(row.trigger_refs or []),
        filter_verdict=row.filter_verdict,
        model=row.model,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        duration_ms=row.duration_ms,
        outcome=row.outcome,
        error=row.error,
        output=row.output,
        sinks_fired=list(row.sinks_fired or []),
        artifacts=[EntityRef.model_validate(a) for a in (row.artifacts or [])],
    )


@dataclass(frozen=True)
class QueuedTrigger:
    """A pending trigger handed to the runner: the queue row's id plus what caused it."""

    id: int
    tenant: str
    automation_id: str
    event_id: int | None
    summary: str
    created_at: datetime


class AutomationStore:
    """CRUD for the tenant-scoped automations and their run ledger."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the automations tables if they do not exist, then reconcile added columns.

        ``create_all`` never alters an existing table, so the columns added after #682 shipped
        (``sink_config`` on automations, ``artifacts`` on the run ledger — both #672) are added
        in place via the shared additive helper (ADR-0067). Idempotent on every startup.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Add the post-#682 columns an older deployment's tables still lack (ADR-0067)."""
        ensure_columns(sync_conn, _StoredAutomation.__table__, _ADDED_AUTOMATION_COLUMNS)
        ensure_columns(sync_conn, _StoredRun.__table__, _ADDED_RUN_COLUMNS)

    async def create(
        self,
        *,
        tenant: str,
        name: str,
        prompt: str,
        autonomy: AutonomyLevel,
        source: str = "user",
        event_trigger: EventTrigger | None = None,
        schedule_trigger: ScheduleTrigger | None = None,
        model: str | None = None,
        sinks: list[Sink] | None = None,
        chat_mode: ChatMode = "rolling",
        chat_session_id: str | None = None,
        rate_cap_per_hour: int = 0,
        digest_window_minutes: int = 0,
        notes_target: DocumentTarget | None = None,
        kb_target: DocumentTarget | None = None,
        enabled: bool = True,
    ) -> Automation:
        """Stage a new automation and return it. Validate before calling."""
        async with self._session() as session:
            row = _StoredAutomation(
                id=uuid.uuid4().hex,
                tenant=tenant,
                name=name,
                enabled=enabled,
                source=source,
                event_trigger=_trigger_to_json(event_trigger) if event_trigger else None,
                schedule_trigger=_schedule_to_json(schedule_trigger) if schedule_trigger else None,
                prompt=prompt,
                model=model,
                autonomy=autonomy,
                sinks=list(sinks or []),
                chat_mode=chat_mode,
                chat_session_id=chat_session_id,
                rate_cap_per_hour=rate_cap_per_hour,
                digest_window_minutes=digest_window_minutes,
                sink_config=_sink_config_to_json(notes_target, kb_target),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_value(row)

    # ── every list[...]-returning method must precede `list()` ───────────────
    #
    # A method named ``list`` shadows the builtin for every *subsequent* annotation in this
    # class body, so a later ``-> list[Automation]`` resolves to the method and mypy
    # rejects it ("not valid as a type"). ``ScheduledTurnStore`` documents the same trap;
    # this class has three such methods rather than one, so they are grouped here.

    async def list_enabled(self) -> list[Automation]:
        """Every enabled automation across all tenants — what a tick evaluates."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredAutomation).where(_StoredAutomation.enabled.is_(True))
            )
            return [_to_value(row) for row in rows]

    async def runs(
        self,
        *,
        tenant: str,
        automation_id: str | None = None,
        outcome: str | None = None,
        limit: int = 100,
    ) -> list[AutomationRun]:
        """The newest ledger entries first, optionally filtered.

        *outcome* narrows to one ledger state (``ok`` / ``error`` / ``skipped``) — the
        runs feed's server-side filter (#669), so a tab watching for failures never
        receives the traffic it would throw away.
        """
        async with self._session() as session:
            stmt = (
                select(_StoredRun)
                .where(_StoredRun.tenant == tenant)
                .order_by(_StoredRun.pk.desc())
                .limit(limit)
            )
            if automation_id:
                stmt = stmt.where(_StoredRun.automation_id == automation_id)
            if outcome:
                stmt = stmt.where(_StoredRun.outcome == outcome)
            rows = await session.scalars(stmt)
            return [_run_to_value(row) for row in rows]

    async def update(
        self,
        *,
        tenant: str,
        automation_id: str,
        name: str,
        prompt: str,
        autonomy: AutonomyLevel,
        event_trigger: EventTrigger | None = None,
        schedule_trigger: ScheduleTrigger | None = None,
        model: str | None = None,
        sinks: list[Sink] | None = None,
        chat_mode: ChatMode = "rolling",
        rate_cap_per_hour: int = 0,
        digest_window_minutes: int = 0,
        notes_target: DocumentTarget | None = None,
        kb_target: DocumentTarget | None = None,
        enabled: bool = True,
    ) -> Automation | None:
        """Replace an automation's editable fields (#668). Validate before calling.

        The Automations page's save: every field the operator edits, in one write, so a
        half-applied edit can't leave a row the runner half-recognises. What it never
        touches: ``source`` (provenance — an instantiated template stays
        ``template:<module>`` however much it is edited), ``chat_session_id`` (the rolling
        chat sink's continuity), ``created_at``, and the last-run stamps (runtime history,
        not configuration). ``None`` if the row does not exist.
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredAutomation).where(
                    _StoredAutomation.tenant == tenant, _StoredAutomation.id == automation_id
                )
            )
            if row is None:
                return None
            row.name = name
            row.enabled = enabled
            row.event_trigger = _trigger_to_json(event_trigger) if event_trigger else None
            row.schedule_trigger = _schedule_to_json(schedule_trigger) if schedule_trigger else None
            row.prompt = prompt
            row.model = model
            row.autonomy = autonomy
            row.sinks = list(sinks or [])
            row.chat_mode = chat_mode
            row.rate_cap_per_hour = rate_cap_per_hour
            row.digest_window_minutes = digest_window_minutes
            row.sink_config = _sink_config_to_json(notes_target, kb_target)
            await session.commit()
            await session.refresh(row)
            return _to_value(row)

    async def list(self, *, tenant: str) -> list[Automation]:
        """All of a tenant's automations, oldest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredAutomation)
                .where(_StoredAutomation.tenant == tenant)
                .order_by(_StoredAutomation.pk)
            )
            return [_to_value(row) for row in rows]

    async def get(self, *, tenant: str, automation_id: str) -> Automation | None:
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredAutomation).where(
                    _StoredAutomation.tenant == tenant, _StoredAutomation.id == automation_id
                )
            )
            return _to_value(row) if row is not None else None

    async def get_any_tenant(self, *, automation_id: str) -> Automation | None:
        """Look up by opaque id alone — for the runner, which drains every tenant."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredAutomation).where(_StoredAutomation.id == automation_id)
            )
            return _to_value(row) if row is not None else None

    async def set_enabled(self, *, tenant: str, automation_id: str, enabled: bool) -> bool:
        """Pause/resume. True if a row was found and updated."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredAutomation).where(
                    _StoredAutomation.tenant == tenant, _StoredAutomation.id == automation_id
                )
            )
            if row is None:
                return False
            row.enabled = enabled
            await session.commit()
            return True

    async def delete(self, *, tenant: str, automation_id: str) -> bool:
        """Remove an automation. True if a row was found and deleted."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredAutomation).where(
                    _StoredAutomation.tenant == tenant, _StoredAutomation.id == automation_id
                )
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def mark_run(self, *, automation_id: str, status: str, ran_at: datetime) -> None:
        """Record the outcome of a run (or a skip) against its automation row.

        Keys on the opaque ``id``, not ``session.get`` (which keys on the internal ``pk``).
        A row deleted between being read and being run is a silent no-op.
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredAutomation).where(_StoredAutomation.id == automation_id)
            )
            if row is None:
                return
            row.last_run_at = ran_at
            row.last_status = status[:255]
            await session.commit()

    # ── the ledger ───────────────────────────────────────────────────────────

    async def record_run(self, run: AutomationRun) -> AutomationRun:
        """Append a ledger entry.

        Always called, at every autonomy level: for ``silent_act`` this is the *only*
        record that anything happened, and for the rest it is the audit trail behind
        whatever a sink announced.
        """
        async with self._session() as session:
            row = _StoredRun(
                id=run.id or uuid.uuid4().hex,
                tenant=run.tenant,
                automation_id=run.automation_id,
                started_at=run.started_at,
                trigger_refs=list(run.trigger_refs),
                filter_verdict=run.filter_verdict,
                model=run.model,
                prompt_tokens=run.prompt_tokens,
                completion_tokens=run.completion_tokens,
                duration_ms=run.duration_ms,
                outcome=run.outcome,
                error=run.error,
                output=run.output,
                sinks_fired=list(run.sinks_fired),
                artifacts=[ref.model_dump() for ref in run.artifacts],
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _run_to_value(row)

    async def runs_since(self, *, automation_id: str, since: datetime) -> int:
        """How many runs an automation has had since *since* — the rate cap's input.

        Counts *runs*, not successes: an automation failing in a loop is exactly what a
        rate cap is for, so a failure must consume budget too.
        """
        async with self._session() as session:
            return (
                await session.scalar(
                    select(func.count())
                    .select_from(_StoredRun)
                    .where(
                        _StoredRun.automation_id == automation_id, _StoredRun.started_at >= since
                    )
                )
                or 0
            )


class AutomationQueue:
    """Durable FIFO of matched triggers awaiting a run (the ADR-0051 pattern).

    Why a table and not an in-memory list: the matcher runs on the event intake, the run
    happens later (possibly much later, if a digest window is open), and a restart in
    between must not lose the trigger. The nightly extraction drain made the same call for
    the same reason.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def enqueue(
        self, *, tenant: str, automation_id: str, event_id: int | None, summary: str
    ) -> int:
        """Append a matched trigger; returns its queue id."""
        async with self._session() as session:
            item = _StoredQueueItem(
                tenant=tenant,
                automation_id=automation_id,
                event_id=event_id,
                summary=summary[:2000],
            )
            session.add(item)
            await session.commit()
            return item.id

    async def pending(self, *, automation_id: str | None = None) -> list[QueuedTrigger]:
        """Oldest pending triggers first (FIFO), optionally for one automation."""
        async with self._session() as session:
            stmt = select(_StoredQueueItem).order_by(_StoredQueueItem.id)
            if automation_id:
                stmt = stmt.where(_StoredQueueItem.automation_id == automation_id)
            rows = await session.scalars(stmt)
            return [
                QueuedTrigger(
                    id=row.id,
                    tenant=row.tenant,
                    automation_id=row.automation_id,
                    event_id=row.event_id,
                    summary=row.summary,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    async def automation_ids(self) -> list[str]:
        """Distinct automation ids with pending work — what a drain tick iterates."""
        async with self._session() as session:
            rows = await session.scalars(select(_StoredQueueItem.automation_id).distinct())
            return list(rows)

    async def oldest_at(self, *, automation_id: str) -> datetime | None:
        """When the oldest pending trigger for an automation arrived.

        The digest window is measured from this: it opens when the *first* unhandled event
        lands, not the last. Measuring from the last would let a steady trickle keep
        resetting the timer, and the digest would never fire at all.
        """
        async with self._session() as session:
            oldest: datetime | None = await session.scalar(
                select(func.min(_StoredQueueItem.created_at)).where(
                    _StoredQueueItem.automation_id == automation_id
                )
            )
            return oldest

    async def delete(self, ids: list[int]) -> int:
        """Remove handled triggers; returns how many rows went."""
        if not ids:
            return 0
        async with self._session() as session:
            result = await session.execute(
                delete(_StoredQueueItem).where(_StoredQueueItem.id.in_(ids))
            )
            await session.commit()
            return cast("CursorResult[Any]", result).rowcount or 0

    async def count(self, *, automation_id: str | None = None) -> int:
        async with self._session() as session:
            stmt = select(func.count()).select_from(_StoredQueueItem)
            if automation_id:
                stmt = stmt.where(_StoredQueueItem.automation_id == automation_id)
            return await session.scalar(stmt) or 0


class KillSwitchStore:
    """The tenant's global automations stop.

    Postgres rather than memory — the deliberate departure from ``PowerController``, which
    resets to running on restart. That is fine for a pause you flip while watching; it is
    not fine for "stop doing things until I work out what went wrong", where a core restart
    silently resuming every automation is the worst possible behaviour.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def halted(self, *, tenant: str) -> bool:
        """Whether automations are stopped for *tenant*. No row = running."""
        async with self._session() as session:
            row = await session.get(_StoredKillSwitch, tenant)
            return bool(row.halted) if row is not None else False

    async def set_halted(self, *, tenant: str, halted: bool) -> None:
        """Stop or resume every automation for *tenant*."""
        async with self._session() as session:
            row = await session.get(_StoredKillSwitch, tenant)
            if row is None:
                session.add(_StoredKillSwitch(tenant=tenant, halted=halted))
            else:
                row.halted = halted
                row.updated_at = datetime.now(UTC)
            await session.commit()
        log.info("automation kill switch set", tenant=tenant, halted=halted)


@dataclass(frozen=True)
class SessionMeta:
    """The automation a chat session belongs to (#672) — the chat list's icon/name/grouping key."""

    session_id: str
    automation_id: str
    name: str
    chat_mode: str


class AutomationSessionStore:
    """Records which chat sessions an automation's chat sink wrote into (#672).

    Only chat-sink runs record here (the runner calls :meth:`record`), so the chat list can show
    an automation's sessions with its icon + name and group a ``per_run`` automation's sessions
    under it — while an ordinary user chat, absent from this table, renders exactly as before.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def record(
        self, *, tenant: str, session_id: str, automation_id: str, name: str, chat_mode: str
    ) -> None:
        """Upsert the session→automation mapping (idempotent for a rolling session's reruns)."""
        async with self._session() as session:
            row = await session.get(_StoredAutomationSession, session_id)
            if row is None:
                session.add(
                    _StoredAutomationSession(
                        session_id=session_id,
                        tenant=tenant,
                        automation_id=automation_id,
                        name=name[:200],
                        chat_mode=chat_mode,
                    )
                )
            else:
                row.automation_id = automation_id
                row.name = name[:200]
                row.chat_mode = chat_mode
            await session.commit()

    async def lookup(self, *, tenant: str, session_ids: list[str]) -> dict[str, SessionMeta]:
        """Map each of *session_ids* that belongs to an automation to its metadata."""
        if not session_ids:
            return {}
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredAutomationSession).where(
                    _StoredAutomationSession.tenant == tenant,
                    _StoredAutomationSession.session_id.in_(session_ids),
                )
            )
            return {
                row.session_id: SessionMeta(
                    session_id=row.session_id,
                    automation_id=row.automation_id,
                    name=row.name,
                    chat_mode=row.chat_mode,
                )
                for row in rows
            }

    async def delete_for_automation(self, *, tenant: str, automation_id: str) -> int:
        """Drop an automation's session mappings (when it is deleted). Returns rows removed."""
        async with self._session() as session:
            result = await session.execute(
                delete(_StoredAutomationSession).where(
                    _StoredAutomationSession.tenant == tenant,
                    _StoredAutomationSession.automation_id == automation_id,
                )
            )
            await session.commit()
            return cast("CursorResult[Any]", result).rowcount or 0


def rate_cap_window_start(now: datetime) -> datetime:
    """The start of the rolling hour a rate cap counts within."""
    return now - timedelta(hours=1)


__all__ = [
    "AutomationQueue",
    "AutomationSessionStore",
    "AutomationStore",
    "KillSwitchStore",
    "QueuedTrigger",
    "SessionMeta",
    "rate_cap_window_start",
]
