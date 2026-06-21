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
    inspect,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import get_logger
from epicurus_tasks.models import Task

log = get_logger("epicurus_tasks.db")

_TaskStatus = Literal["open", "in_progress", "done"]

# Columns added after the table's first release (#218, v0.5.0). create_all never
# alters an existing table, so a database provisioned before then lacks these; they
# are reconciled in place at startup by ``TaskStore._ensure_columns``.
_ADDED_COLUMNS = ("status", "priority", "tags")


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
        """Idempotently add columns introduced after the table's first release.

        There is no migration framework — the store uses ``create_all``, which builds a
        missing table but never alters an existing one. On a database provisioned before
        #218 added the ``status`` / ``priority`` / ``tags`` fields (v0.5.0) those columns
        are missing, so every task read 500s on Postgres with
        ``column tasks_local.status does not exist`` — not just the board, but
        ``tasks_list``, the attachment picker, and the resolver, which all SELECT them.
        This adds the missing columns in place, compiling a per-dialect type so it is
        portable across Postgres and the tests' SQLite. Additive only: drops, renames,
        type changes, and NOT NULL backfills still need a real migration.

        Mirrors ``LlmPrefsStore._ensure_columns`` — the same drift class for the same
        reason (the store has no Alembic).
        """
        inspector = inspect(sync_conn)
        existing = {col["name"] for col in inspector.get_columns(_StoredTask.__tablename__)}
        for name in _ADDED_COLUMNS:
            if name in existing:
                continue
            type_sql = _StoredTask.__table__.c[name].type.compile(dialect=sync_conn.dialect)
            sync_conn.exec_driver_sql(
                f"ALTER TABLE {_StoredTask.__tablename__} ADD COLUMN {name} {type_sql}"
            )
            log.info("reconciled tasks_local: added missing column", column=name)

    async def list_tasks(self, *, tenant_id: str) -> list[Task]:
        """Return all open tasks for *tenant_id*, newest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredTask)
                .where(
                    _StoredTask.tenant_id == tenant_id,
                    # A task is open when the legacy completed flag is False (the status
                    # column is nullable and can't be used as the primary filter without
                    # a full backfill migration).
                    _StoredTask.completed.is_(False),
                )
                .order_by(_StoredTask.created_at.desc())
            )
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
    ) -> Task:
        """Patch a task's editable fields and return it.

        Only the fields passed (non-``None``) are changed; the rest keep their
        current value. Raises :exc:`KeyError` if the task does not exist.
        """
        values: dict[str, object] = {}
        if title is not None:
            values["title"] = title
        if notes is not None:
            values["notes"] = notes
        if due is not None:
            values["due"] = due
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
