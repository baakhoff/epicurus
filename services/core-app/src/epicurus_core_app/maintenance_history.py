"""Persisted maintenance-run history (#733) — the durable record `MaintenanceOrchestrator.
last_run` never was.

``MaintenanceOrchestrator``'s own ``last_run()`` is a single in-memory slot: correct for "what
just happened" but gone on restart, and it never distinguished a scheduled fire from a manual
"run now" click. :class:`MaintenanceRunStore` is the durable counterpart — one row per
completed batch, written via the orchestrator's ``on_recorded`` hook (mirrors
``AutomationRunner.on_recorded`` → the live runs feed) on **every** completion, including a
shutdown-interrupted one (``MaintenanceOrchestrator.shutdown`` calls it directly, since
``_drive``'s own ``except CancelledError`` cannot safely ``await`` again once caught — see its
docstring). A crashed batch leaves a row, not a mystery.

Retention is both a row cap and an age cutoff (:meth:`MaintenanceRunStore.prune`, wired as
its own maintenance job — ``prune_run_history_job`` in ``maintenance.py``) — whichever trims
more, the same "count or age, not one alone" posture the module-event log and the
notification center each apply to their own retention question.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import DateTime, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core_app.maintenance import MaintenanceJobResult, MaintenanceRun

__all__ = ["MaintenanceRunPage", "MaintenanceRunRecord", "MaintenanceRunStore"]

_DEFAULT_MAX_ROWS = 200
_DEFAULT_MAX_AGE_DAYS = 90


@dataclass(frozen=True)
class MaintenanceRunRecord:
    """One durable history row — an immutable value returned by the store."""

    id: int
    tenant: str
    started_at: str
    finished_at: str
    scope: Literal["all", "nightly"]
    source: Literal["scheduled", "manual"]
    jobs: list[MaintenanceJobResult]


@dataclass(frozen=True)
class MaintenanceRunPage:
    """One page of history, newest-first. ``next_cursor`` is ``None`` past the last page."""

    runs: list[MaintenanceRunRecord]
    next_cursor: int | None


class _Base(DeclarativeBase):
    pass


class _MaintenanceRunRow(_Base):
    """ORM mapping — one row per completed (or interrupted) maintenance batch.

    ``pk`` is the ordering/cursor key (insertion order — this table is append-only, rows are
    never updated), the same role it plays in ``NotificationStore``/``EventLogStore``.
    """

    __tablename__ = "maintenance_runs"

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))
    # JSON-encoded list[{key, label, status, detail}] — a run's job count is small and fixed
    # by the registry, so a normalized child table buys nothing a JSON blob doesn't already
    # give (mirrors NotificationStore.entity_ref_json).
    jobs_json: Mapped[str] = mapped_column(Text)


def _to_record(row: _MaintenanceRunRow) -> MaintenanceRunRecord:
    raw = json.loads(row.jobs_json)
    jobs = [
        MaintenanceJobResult(key=j["key"], label=j["label"], status=j["status"], detail=j["detail"])
        for j in raw
    ]
    return MaintenanceRunRecord(
        id=row.pk,
        tenant=row.tenant,
        started_at=row.started_at.isoformat(),
        finished_at=row.finished_at.isoformat(),
        scope=row.scope,  # type: ignore[arg-type]
        source=row.source,  # type: ignore[arg-type]
        jobs=jobs,
    )


class MaintenanceRunStore:
    """Read/write a tenant's persisted maintenance-run history."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        max_rows: int = _DEFAULT_MAX_ROWS,
        max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)
        self._max_rows = max_rows
        self._max_age_days = max_age_days

    async def init(self) -> None:
        """Create the schema (idempotent).

        No ``ensure_columns`` call: this table is new in this release, so it has no deployed
        predecessor to reconcile against — the first column added *after* this ships must add
        one (ADR-0067).
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def record(self, run: MaintenanceRun) -> None:
        """``MaintenanceOrchestrator``'s ``on_recorded`` hook — one row per batch.

        ``run.tenant``/``run.source``/``run.finished_at`` are #733's additions to
        ``MaintenanceRun``; every caller that reaches here (the orchestrator) always sets
        them, so there is nothing optional to default here.
        """
        async with self._session() as session:
            session.add(
                _MaintenanceRunRow(
                    tenant=run.tenant,
                    started_at=datetime.fromisoformat(run.ran_at),
                    finished_at=datetime.fromisoformat(run.finished_at),
                    scope=run.scope,
                    source=run.source,
                    jobs_json=json.dumps(
                        [
                            {"key": j.key, "label": j.label, "status": j.status, "detail": j.detail}
                            for j in run.jobs
                        ]
                    ),
                )
            )
            await session.commit()

    async def most_recent(self, tenant: str) -> MaintenanceRunRecord | None:
        """The tenant's newest history row, or ``None`` — backs the status GET's ``last_run``
        (survives a restart, unlike ``MaintenanceOrchestrator.last_run()``)."""
        async with self._session() as session:
            row = await session.scalar(
                select(_MaintenanceRunRow)
                .where(_MaintenanceRunRow.tenant == tenant)
                .order_by(_MaintenanceRunRow.pk.desc())
            )
            return _to_record(row) if row is not None else None

    async def page(self, tenant: str, *, cursor: int | None, limit: int) -> MaintenanceRunPage:
        """Up to *limit* rows newest-first, starting *before* *cursor* (an opaque row id from a
        previous page's ``next_cursor``); ``cursor=None`` starts from the newest row."""
        async with self._session() as session:
            stmt = select(_MaintenanceRunRow).where(_MaintenanceRunRow.tenant == tenant)
            if cursor is not None:
                stmt = stmt.where(_MaintenanceRunRow.pk < cursor)
            stmt = stmt.order_by(_MaintenanceRunRow.pk.desc()).limit(limit + 1)
            rows = list(await session.scalars(stmt))
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            next_cursor = page_rows[-1].pk if has_more and page_rows else None
            return MaintenanceRunPage(
                runs=[_to_record(r) for r in page_rows], next_cursor=next_cursor
            )

    async def prune(self, tenant: str) -> int:
        """Drop rows past the retention cap — beyond ``max_rows`` most-recent, or older than
        ``max_age_days`` — whichever condition catches a given row. Returns the count dropped.

        Suitable as a maintenance job itself (``prune_run_history_job``): light, idempotent,
        safe on the nightly batch.
        """
        async with self._session() as session:
            cutoff = datetime.now(UTC) - timedelta(days=self._max_age_days)
            stale_pks = set(
                await session.scalars(
                    select(_MaintenanceRunRow.pk).where(
                        _MaintenanceRunRow.tenant == tenant,
                        _MaintenanceRunRow.finished_at < cutoff,
                    )
                )
            )
            count = (
                await session.scalar(
                    select(func.count())
                    .select_from(_MaintenanceRunRow)
                    .where(_MaintenanceRunRow.tenant == tenant)
                )
                or 0
            )
            excess = count - self._max_rows
            if excess > 0:
                stale_pks |= set(
                    await session.scalars(
                        select(_MaintenanceRunRow.pk)
                        .where(_MaintenanceRunRow.tenant == tenant)
                        .order_by(_MaintenanceRunRow.pk)
                        .limit(excess)
                    )
                )
            if not stale_pks:
                return 0
            await session.execute(
                delete(_MaintenanceRunRow).where(_MaintenanceRunRow.pk.in_(stale_pks))
            )
            await session.commit()
            return len(stale_pks)
