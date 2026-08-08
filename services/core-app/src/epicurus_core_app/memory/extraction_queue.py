"""A durable queue of finished exchanges awaiting fact extraction (ADR-0051).

The *deferred* path of the memory design: instead of distilling user facts inline after every
turn — a full LLM call that then competes with the user's *next* turn for the one local GPU —
the agent drops the exchange here, and the nightly
:class:`~epicurus_core_app.memory.extraction.ExtractionRunner` drains it when nothing is waiting
on the model. The queue is in Postgres so a restart never loses a pending exchange.

The row holds just the text the extractor needs (the latest user message and the assistant
reply) plus when it was enqueued — never the whole transcript. It shares the conversation
store's :class:`~epicurus_core_app.memory.store.Base`, so its table is created by
``ConversationStore.init`` like the other core tables; :meth:`ExtractionQueue.init` also creates
it so the queue is self-sufficient (and easy to stand up in a test).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import CursorResult, DateTime, String, Text, delete, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from epicurus_core.db import ensure_columns
from epicurus_core_app.memory.store import Base

# Columns added to memory_extraction_queue after the table's first release, reconciled in place
# at init (the store has no migration framework — ADR-0067). ``session_id`` (#771) stamps each
# exchange with the conversation it came from, so deleting a chat can purge its still-queued
# exchanges; legacy rows stay NULL and drain exactly as before.
_ADDED_COLUMNS = ("session_id",)


class ExtractionTask(Base):
    """One finished exchange awaiting background fact extraction (tenant-scoped)."""

    __tablename__ = "memory_extraction_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    user_text: Mapped[str] = mapped_column(Text)
    assistant_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The session the exchange came from (#771), so a deleted chat's queued exchanges can be
    # purged before the nightly drain distils them. Nullable: rows enqueued before this column
    # existed (or by a caller with no session) drain as before — they just can't be targeted.
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)


class QueuedExchange(BaseModel):
    """A pending exchange handed to the extractor: the text plus its queue id.

    ``session_id`` is ``None`` for rows enqueued before #771 stamped the queue (they drain
    exactly as before) and for callers with no session.
    """

    id: int
    tenant: str
    user_text: str
    assistant_text: str
    created_at: datetime | None = None
    session_id: str | None = None


class ExtractionQueue:
    """Durable FIFO of exchanges awaiting fact extraction (Postgres, tenant-scoped)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the queue table if it does not exist (idempotent; shares the store's Base),
        then reconcile columns added after first release (``session_id``, #771)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Add post-release columns to an already-provisioned table (ADR-0067)."""
        ensure_columns(sync_conn, ExtractionTask.__table__, _ADDED_COLUMNS)

    async def enqueue(
        self, *, tenant: str, user_text: str, assistant_text: str, session_id: str | None = None
    ) -> int | None:
        """Append an exchange to the queue; returns its id (None if there's nothing to learn).

        An empty user message carries no durable fact, so it is dropped here rather than
        queued for an extraction that the extractor would skip anyway. ``session_id`` stamps
        the exchange with its conversation (#771) so a deleted chat can purge what it queued.
        """
        if not user_text.strip():
            return None
        async with self._session() as session:
            task = ExtractionTask(
                tenant=tenant,
                user_text=user_text,
                assistant_text=assistant_text,
                session_id=session_id,
            )
            session.add(task)
            await session.commit()
            return task.id

    async def pending(self, *, limit: int, tenant: str | None = None) -> list[QueuedExchange]:
        """The oldest pending exchanges first (FIFO), capped at ``limit``.

        Optionally scoped to one ``tenant``; the runner drains every tenant, so it passes none.
        """
        async with self._session() as session:
            stmt = select(ExtractionTask).order_by(ExtractionTask.id).limit(limit)
            if tenant is not None:
                stmt = stmt.where(ExtractionTask.tenant == tenant)
            rows = await session.scalars(stmt)
            return [
                QueuedExchange(
                    id=row.id,
                    tenant=row.tenant,
                    user_text=row.user_text,
                    assistant_text=row.assistant_text,
                    created_at=row.created_at,
                    session_id=row.session_id,
                )
                for row in rows
            ]

    async def delete_for_session(self, *, tenant: str, session_id: str) -> int:
        """Purge every still-queued exchange of one conversation (#771); returns rows removed.

        The delete-cascade's queue step: without it, a "deleted" chat's exchanges — which carry
        the conversation *text* — would still be distilled into memory facts that night. Only
        stamped rows can match; legacy NULL-``session_id`` rows are untouched (they cannot be
        attributed to a session and drain as before). Tenant-scoped (constraint #1).
        """
        async with self._session() as session:
            result = await session.execute(
                delete(ExtractionTask).where(
                    ExtractionTask.tenant == tenant,
                    ExtractionTask.session_id == session_id,
                )
            )
            await session.commit()
            return cast("CursorResult[Any]", result).rowcount or 0

    async def delete(self, ids: list[int]) -> int:
        """Remove processed exchanges from the queue; returns how many rows were removed."""
        if not ids:
            return 0
        async with self._session() as session:
            result = await session.execute(delete(ExtractionTask).where(ExtractionTask.id.in_(ids)))
            await session.commit()
            return cast("CursorResult[Any]", result).rowcount or 0

    async def count(self, *, tenant: str | None = None) -> int:
        """How many exchanges are waiting (optionally for one tenant)."""
        async with self._session() as session:
            stmt = select(func.count()).select_from(ExtractionTask)
            if tenant is not None:
                stmt = stmt.where(ExtractionTask.tenant == tenant)
            return await session.scalar(stmt) or 0
