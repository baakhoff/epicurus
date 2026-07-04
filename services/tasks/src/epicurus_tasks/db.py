"""SQLAlchemy schema for the local tasks store (tenant-scoped)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import (
    Boolean,
    DateTime,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns
from epicurus_tasks.models import Task, TaskScope

_TaskStatus = Literal["open", "in_progress", "done"]

# Columns added after the table's first release. create_all never alters an existing table,
# so a database provisioned before a column existed lacks it; they are reconciled in place at
# startup by ``TaskStore._ensure_columns``. ``status``/``priority``/``tags`` arrived in v0.5.0
# (#218); ``repeat`` (the local recurrence rule) in v0.14.0 (#471, ADR-0082).
_ADDED_COLUMNS = ("status", "priority", "tags", "repeat")


class _Base(DeclarativeBase):
    pass


class _StoredTask(_Base):
    """ORM mapping for a single local task, scoped by tenant."""

    __tablename__ = "tasks_local"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_tasks_tenant_id"),)

    pk: Mapped[int] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(String(255), index=True)
    tenant_id: Mapped[str] = mapped_column(String(63), index=True)
    title: Mapped[str] = mapped_column(String(1024))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    due: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Richer fields (added in v0.5.0)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    # Recurrence rule (added v0.14.0, #471): a bare RFC 5545 RRULE on a recurring task; NULL
    # for a one-off. Emulated module-side (Google Tasks has no recurrence field) — the local
    # store keeps it in-row, Google in a side table (ADR-0082).
    repeat: Mapped[str | None] = mapped_column(Text, nullable=True)


def _row_to_task(row: _StoredTask) -> Task:
    raw_tags: list[str] = json.loads(row.tags) if row.tags else []
    # status column takes precedence; fall back to the legacy completed bool for rows
    # written before v0.5.0 that have no status column value yet.
    if row.status in ("open", "in_progress", "done"):
        status: _TaskStatus = cast(_TaskStatus, row.status)
    else:
        status = "done" if row.completed else "open"
    return Task(
        id=row.id,
        title=row.title,
        notes=row.notes,
        due=row.due,
        status=status,
        completed_at=row.completed_at,
        priority=row.priority,  # type: ignore[arg-type]
        tags=raw_tags,
        repeat=row.repeat,
    )


class TaskStore:
    """CRUD helpers for the tenant-scoped local task store in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Reconcile columns added after first release via the shared additive helper (#249).

        ``status`` / ``priority`` / ``tags`` arrived in v0.5.0 (#218); a database provisioned
        before then lacks them and every task read 500s on Postgres until they are added in
        place. See :func:`epicurus_core.db.ensure_columns`.
        """
        ensure_columns(sync_conn, _StoredTask.__table__, _ADDED_COLUMNS)

    async def list_tasks(self, *, tenant_id: str, scope: TaskScope = "open") -> list[Task]:
        """Return tasks for *tenant_id*, newest first, filtered by *scope* (ADR-0049).

        ``"open"`` (default) returns un-completed tasks, ``"done"`` only completed ones,
        ``"all"`` both. The filter is on the legacy ``completed`` flag — the ``status``
        column is nullable and can't be the primary filter without a backfill migration —
        which ``complete_task`` / ``update_task`` always keep in sync with ``status``.
        """
        stmt = select(_StoredTask).where(_StoredTask.tenant_id == tenant_id)
        if scope == "open":
            stmt = stmt.where(_StoredTask.completed.is_(False))
        elif scope == "done":
            stmt = stmt.where(_StoredTask.completed.is_(True))
        async with self._session() as session:
            rows = await session.scalars(stmt.order_by(_StoredTask.created_at.desc()))
            return [_row_to_task(r) for r in rows]

    async def add_task(
        self,
        *,
        tenant_id: str,
        title: str,
        notes: str | None,
        due: str | None,
        status: str = "open",
        priority: str | None = None,
        tags: list[str] | None = None,
        repeat: str | None = None,
    ) -> Task:
        """Insert a new task and return it."""
        task_id = str(uuid.uuid4())
        row = _StoredTask(
            id=task_id,
            tenant_id=tenant_id,
            title=title,
            notes=notes,
            due=due,
            completed=status == "done",
            status=status,
            priority=priority,
            tags=json.dumps(tags or []),
            repeat=repeat or None,
        )
        async with self._session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _row_to_task(row)

    async def complete_task(self, *, tenant_id: str, task_id: str) -> Task:
        """Mark a task complete.

        Raises :exc:`KeyError` if the task does not exist for this tenant.
        """
        now = datetime.now(UTC).isoformat()
        async with self._session() as session:
            result = await session.execute(
                update(_StoredTask)
                .where(_StoredTask.tenant_id == tenant_id, _StoredTask.id == task_id)
                .values(completed=True, completed_at=now, status="done")
                .returning(_StoredTask)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise KeyError(f"task {task_id!r} not found for tenant {tenant_id!r}")
            await session.commit()
        return _row_to_task(row)

    async def update_task(
        self,
        *,
        tenant_id: str,
        task_id: str,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        tags: list[str] | None = None,
        repeat: str | None = None,
    ) -> Task:
        """Patch a task's editable fields and return it.

        Only the fields passed (non-``None``) are changed; the rest keep their
        current value. An empty string clears ``due`` / ``notes`` / ``repeat`` to
        ``NULL`` rather than storing a literal empty string (#475; ``repeat`` #471).
        Raises :exc:`KeyError` if the task does not exist.
        """
        values: dict[str, object] = {}
        if title is not None:
            values["title"] = title
        if notes is not None:
            values["notes"] = notes or None
        if due is not None:
            values["due"] = due or None
        if repeat is not None:
            values["repeat"] = repeat or None  # "" clears the recurrence (turns it one-off)
        if status is not None:
            values["status"] = status
            # Keep the legacy completed flag in sync so list_tasks still works.
            values["completed"] = status == "done"
            if status == "done":
                values.setdefault("completed_at", datetime.now(UTC).isoformat())
        if priority is not None:
            values["priority"] = priority
        if tags is not None:
            values["tags"] = json.dumps(tags)

        if not values:
            current = await self.get_task(tenant_id=tenant_id, task_id=task_id)
            if current is None:
                raise KeyError(f"task {task_id!r} not found for tenant {tenant_id!r}")
            return current

        async with self._session() as session:
            result = await session.execute(
                update(_StoredTask)
                .where(_StoredTask.tenant_id == tenant_id, _StoredTask.id == task_id)
                .values(**values)
                .returning(_StoredTask)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise KeyError(f"task {task_id!r} not found for tenant {tenant_id!r}")
            await session.commit()
        return _row_to_task(row)

    async def get_task(self, *, tenant_id: str, task_id: str) -> Task | None:
        """Return a single task, or ``None`` if it doesn't exist."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredTask).where(
                    _StoredTask.tenant_id == tenant_id,
                    _StoredTask.id == task_id,
                )
            )
            return _row_to_task(row) if row is not None else None

    async def delete_task(self, *, tenant_id: str, task_id: str) -> None:
        """Delete a task (hard-delete; for tests and cleanup)."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredTask).where(
                    _StoredTask.tenant_id == tenant_id,
                    _StoredTask.id == task_id,
                )
            )
            await session.commit()


class _StoredRepeat(_Base):
    """An emulated recurrence rule for an *external-provider* task, tenant-scoped (ADR-0082).

    Google Tasks has no recurrence field, so a repeating Google task's RRULE is kept here,
    keyed by the provider list + task id. The local store keeps its own rule in-row
    (``tasks_local.repeat``) and never touches this table. In the shared ``_Base`` metadata,
    so ``TaskStore.init``'s ``create_all`` provisions it alongside ``tasks_local``.
    """

    __tablename__ = "task_repeats"
    __table_args__ = (UniqueConstraint("tenant_id", "list_id", "task_id", name="uq_task_repeats"),)

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(63), index=True)
    list_id: Mapped[str] = mapped_column(String(255))
    task_id: Mapped[str] = mapped_column(String(255), index=True)
    rrule: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepeatStore:
    """Side-table storage of emulated recurrence rules for external-provider tasks (ADR-0082).

    Keyed by ``(tenant_id, list_id, task_id)``. Backs the Google provider, which has no
    recurrence field; the local provider stores its rule in-row and never uses this. Shares
    the module's engine, so the table is created by :meth:`TaskStore.init` (same ``_Base``).
    Writes are delete-then-insert so they work identically on SQLite (tests) and Postgres
    without a dialect-specific upsert.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def get(self, *, tenant_id: str, list_id: str, task_id: str) -> str | None:
        """The rule for one task, or ``None`` if it isn't recurring."""
        async with self._session() as session:
            rule: str | None = await session.scalar(
                select(_StoredRepeat.rrule).where(
                    _StoredRepeat.tenant_id == tenant_id,
                    _StoredRepeat.list_id == list_id,
                    _StoredRepeat.task_id == task_id,
                )
            )
            return rule

    async def get_many(
        self, *, tenant_id: str, list_id: str, task_ids: list[str]
    ) -> dict[str, str]:
        """Rules for many task ids in one query — fills ``task_id -> rrule`` for a list read."""
        if not task_ids:
            return {}
        async with self._session() as session:
            rows = await session.execute(
                select(_StoredRepeat.task_id, _StoredRepeat.rrule).where(
                    _StoredRepeat.tenant_id == tenant_id,
                    _StoredRepeat.list_id == list_id,
                    _StoredRepeat.task_id.in_(task_ids),
                )
            )
            return dict(rows.tuples().all())

    async def set(self, *, tenant_id: str, list_id: str, task_id: str, rrule: str | None) -> None:
        """Upsert (non-empty *rrule*) or clear (``None`` / ``""``) one task's rule."""
        async with self._session() as session:
            await session.execute(
                delete(_StoredRepeat).where(
                    _StoredRepeat.tenant_id == tenant_id,
                    _StoredRepeat.list_id == list_id,
                    _StoredRepeat.task_id == task_id,
                )
            )
            if rrule:
                session.add(
                    _StoredRepeat(
                        tenant_id=tenant_id, list_id=list_id, task_id=task_id, rrule=rrule
                    )
                )
            await session.commit()

    async def delete(self, *, tenant_id: str, list_id: str, task_id: str) -> None:
        """Retire a task's rule — GC when the task is deleted or vanishes from Google."""
        await self.set(tenant_id=tenant_id, list_id=list_id, task_id=task_id, rrule=None)
