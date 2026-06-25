"""Suspended agent runs — the durable state behind ``ask_user`` pause/resume (ADR-0053).

When the model calls ``ask_user`` the turn cannot finish until the operator answers, so the
in-progress run is persisted here (the conversation so far, the pending tool-call id, and the
question), the SSE stream ends, and a resume request rehydrates and continues it. Rows are
**consumed** on resume (taken, not just read) and reaped after a TTL, so an abandoned prompt
never lingers. Tenant-scoped (constraint #1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _Base(DeclarativeBase):
    pass


class _SuspendedRun(_Base):
    """ORM row for one paused turn awaiting a user's answer."""

    __tablename__ = "agent_suspended_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pending_call_id: Mapped[str] = mapped_column(String(255))
    question: Mapped[str] = mapped_column(Text)
    # The conversation up to (and including) the assistant message that called ask_user, plus
    # any sibling tool results — i.e. everything the loop needs to continue once the answer is
    # appended as the pending call's tool result.
    conversation: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@dataclass(frozen=True)
class SuspendedRun:
    """A rehydrated suspended run — enough to resume the turn."""

    session_id: str | None
    model: str | None
    pending_call_id: str
    question: str
    conversation: list[dict[str, Any]]


class SuspendedRunStore:
    """Persist and rehydrate suspended agent runs (tenant-scoped)."""

    def __init__(self, engine: AsyncEngine, *, ttl_hours: int = 24) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)
        self._ttl = timedelta(hours=max(1, ttl_hours))

    async def init(self) -> None:
        """Create the schema."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def save(
        self,
        *,
        tenant: str,
        session_id: str | None,
        model: str | None,
        pending_call_id: str,
        question: str,
        conversation: list[dict[str, Any]],
    ) -> str:
        """Persist a suspended run and return its generated ``run_id``.

        Opportunistically reaps rows older than the TTL first, so an abandoned prompt is
        cleaned up without a separate scheduler.
        """
        run_id = uuid.uuid4().hex
        cutoff = datetime.now(UTC) - self._ttl
        async with self._session() as session:
            await session.execute(delete(_SuspendedRun).where(_SuspendedRun.created_at < cutoff))
            session.add(
                _SuspendedRun(
                    id=run_id,
                    tenant=tenant,
                    session_id=session_id,
                    model=model,
                    pending_call_id=pending_call_id,
                    question=question,
                    conversation=conversation,
                )
            )
            await session.commit()
        return run_id

    async def take(self, *, tenant: str, run_id: str) -> SuspendedRun | None:
        """Return and **delete** the suspended run, or ``None`` if absent/foreign-tenant.

        Consuming on read makes resume idempotent-safe: a double-submit finds nothing the
        second time rather than replaying the turn.
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_SuspendedRun).where(
                    _SuspendedRun.tenant == tenant, _SuspendedRun.id == run_id
                )
            )
            if row is None:
                return None
            data = SuspendedRun(
                session_id=row.session_id,
                model=row.model,
                pending_call_id=row.pending_call_id,
                question=row.question,
                conversation=list(row.conversation),
            )
            await session.delete(row)
            await session.commit()
            return data
