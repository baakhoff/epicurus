"""Conversation persistence — append-only message history in Postgres (tenant-scoped)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class StoredMessage(Base):
    """One persisted message in a conversation."""

    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationStore:
    """Append-only conversation history in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def append(self, *, tenant: str, session_id: str, role: str, content: str) -> int:
        """Persist a message; returns its id (used as the recall point id)."""
        async with self._session() as session:
            message = StoredMessage(
                tenant=tenant, session_id=session_id, role=role, content=content
            )
            session.add(message)
            await session.commit()
            return message.id

    async def history(self, *, tenant: str, session_id: str) -> list[tuple[str, str]]:
        """Return the ``(role, content)`` messages of a session, oldest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(StoredMessage)
                .where(StoredMessage.tenant == tenant, StoredMessage.session_id == session_id)
                .order_by(StoredMessage.created_at, StoredMessage.id)
            )
            return [(message.role, message.content) for message in rows]
