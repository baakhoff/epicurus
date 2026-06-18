"""Persisted LLM preferences — hidden-model list and global default (tenant-scoped).

Stored in the core's Postgres database so preferences survive restarts and are
consistent across devices (unlike the web client's localStorage model pref, which
is per-device and per-chat).  The table is auto-created on first use via
``LlmPrefsStore.init()`` (same pattern as ``ConversationStore``).
"""

from __future__ import annotations

import json
from typing import cast

from sqlalchemy import String, Text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _PrefBase(DeclarativeBase):
    pass


class _LlmPrefRow(_PrefBase):
    """One preferences record per tenant — a single row holds all knobs."""

    __tablename__ = "llm_prefs"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    # JSON-encoded list of model names the operator has hidden from pickers.
    hidden_models: Mapped[str] = mapped_column(Text, default="[]", server_default="'[]'")
    # Operator-chosen global default for chat; NULL means fall back to the env default.
    global_default: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Operator-chosen global default for embedding; NULL means fall back to the env default.
    embed_default: Mapped[str | None] = mapped_column(String(256), nullable=True)


class LlmPrefsStore:
    """Read/write the hidden-model list and global default for a tenant."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_PrefBase.metadata.create_all)

    async def _get_or_create(self, session: AsyncSession, tenant: str) -> _LlmPrefRow:
        row = await session.get(_LlmPrefRow, tenant)
        if row is None:
            row = _LlmPrefRow(tenant=tenant)
            session.add(row)
        return row

    async def get_hidden(self, tenant: str) -> list[str]:
        """Return the list of model names hidden for ``tenant``."""
        async with self._session() as session:
            row = await session.get(_LlmPrefRow, tenant)
            if row is None:
                return []
            return cast("list[str]", json.loads(row.hidden_models or "[]"))

    async def set_hidden(self, tenant: str, models: list[str]) -> None:
        """Replace the entire hidden list for ``tenant``."""
        async with self._session() as session:
            row = await self._get_or_create(session, tenant)
            row.hidden_models = json.dumps(models)
            await session.commit()

    async def get_default(self, tenant: str) -> str | None:
        """Return the stored global default, or ``None`` if unset."""
        async with self._session() as session:
            row = await session.get(_LlmPrefRow, tenant)
            return row.global_default if row is not None else None

    async def set_default(self, tenant: str, model: str | None) -> None:
        """Set or clear the global default for ``tenant``."""
        async with self._session() as session:
            row = await self._get_or_create(session, tenant)
            row.global_default = model
            await session.commit()

    async def get_embed_default(self, tenant: str) -> str | None:
        """Return the stored global embedding default, or ``None`` if unset."""
        async with self._session() as session:
            row = await session.get(_LlmPrefRow, tenant)
            return row.embed_default if row is not None else None

    async def set_embed_default(self, tenant: str, model: str | None) -> None:
        """Set or clear the global embedding default for ``tenant``."""
        async with self._session() as session:
            row = await self._get_or_create(session, tenant)
            row.embed_default = model
            await session.commit()
