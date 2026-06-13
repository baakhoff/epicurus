"""SQLAlchemy schema for the local tasks store (tenant-scoped)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

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
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_tasks.models import Task


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


def _row_to_task(row: _StoredTask) -> Task:
    return Task(
        id=row.id,
        title=row.title,
        notes=row.notes,
        due=row.due,
        completed=row.completed,
        completed_at=row.completed_at,
    )


class TaskStore:
    """CRUD helpers for the tenant-scoped local task store in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def list_tasks(self, *, tenant_id: str) -> list[Task]:
        """Return all tasks for *tenant_id*, newest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredTask)
                .where(_StoredTask.tenant_id == tenant_id, _StoredTask.completed.is_(False))
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
    ) -> Task:
        """Insert a new task and return it."""
        task_id = str(uuid.uuid4())
        row = _StoredTask(
            id=task_id,
            tenant_id=tenant_id,
            title=title,
            notes=notes,
            due=due,
            completed=False,
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
                .values(completed=True, completed_at=now)
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
