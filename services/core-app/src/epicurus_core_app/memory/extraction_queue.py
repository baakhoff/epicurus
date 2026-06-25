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
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from epicurus_core_app.memory.store import Base


class ExtractionTask(Base):
    """One finished exchange awaiting background fact extraction (tenant-scoped)."""

    __tablename__ = "memory_extraction_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    user_text: Mapped[str] = mapped_column(Text)
    assistant_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QueuedExchange(BaseModel):
    """A pending exchange handed to the extractor: the text plus its queue id."""

    id: int
    tenant: str
    user_text: str
    assistant_text: str
    created_at: datetime | None = None


class ExtractionQueue:
    """Durable FIFO of exchanges awaiting fact extraction (Postgres, tenant-scoped)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the queue table if it does not exist (idempotent; shares the store's Base)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def enqueue(self, *, tenant: str, user_text: str, assistant_text: str) -> int | None:
        """Append an exchange to the queue; returns its id (None if there's nothing to learn).

        An empty user message carries no durable fact, so it is dropped here rather than
        queued for an extraction that the extractor would skip anyway.
        """
        if not user_text.strip():
            return None
        async with self._session() as session:
            task = ExtractionTask(tenant=tenant, user_text=user_text, assistant_text=assistant_text)
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
                )
                for row in rows
            ]

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
