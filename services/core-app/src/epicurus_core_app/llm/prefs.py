"""Persisted LLM preferences — hidden-model list and global default (tenant-scoped).

Stored in the core's Postgres database so preferences survive restarts and are
consistent across devices (unlike the web client's localStorage model pref, which
is per-device and per-chat).  The table is auto-created on first use via
``LlmPrefsStore.init()`` (same pattern as ``ConversationStore``).
"""

from __future__ import annotations

import json
from typing import cast

from sqlalchemy import Integer, String, Text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns


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
    # Operator-chosen Ollama context window (num_ctx); NULL means fall back to the env default.
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Operator-chosen Ollama KV-cache type ("f16" | "q8_0" | "q4_0"); NULL = runtime default.
    # Server-wide, applied via the Ollama container env — not live (see ADR-0046).
    kv_cache_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Operator-chosen agent loop bound (tool rounds per turn); NULL = the env default.
    agent_max_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)


class LlmPrefsStore:
    """Read/write the hidden-model list and global default for a tenant."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_PrefBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Reconcile columns added after first release via the shared additive helper (#249).

        ``global_default`` / ``embed_default`` (#214), ``context_window``, ``kv_cache_type``,
        and ``agent_max_steps`` all postdate the table's first release; without this an
        existing ``llm_prefs`` table 500s on every prefs/embedding read. See
        :func:`epicurus_core.db.ensure_columns`.
        """
        ensure_columns(
            sync_conn,
            _LlmPrefRow.__table__,
            (
                "global_default",
                "embed_default",
                "context_window",
                "kv_cache_type",
                "agent_max_steps",
            ),
        )

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

    async def get_context_window(self, tenant: str) -> int | None:
        """Return the stored Ollama context window (num_ctx), or ``None`` if unset."""
        async with self._session() as session:
            row = await session.get(_LlmPrefRow, tenant)
            return row.context_window if row is not None else None

    async def set_context_window(self, tenant: str, value: int | None) -> None:
        """Set or clear the Ollama context window (num_ctx) for ``tenant``."""
        async with self._session() as session:
            row = await self._get_or_create(session, tenant)
            row.context_window = value
            await session.commit()

    async def get_kv_cache_type(self, tenant: str) -> str | None:
        """Return the stored Ollama KV-cache type, or ``None`` if unset."""
        async with self._session() as session:
            row = await session.get(_LlmPrefRow, tenant)
            return row.kv_cache_type if row is not None else None

    async def set_kv_cache_type(self, tenant: str, value: str | None) -> None:
        """Set or clear the operator's preferred Ollama KV-cache type for ``tenant``.

        Server-wide and applied via the Ollama container's ``OLLAMA_KV_CACHE_TYPE`` env, so
        it takes effect only after the runtime restarts (the core cannot restart Ollama —
        ADR-0046). Persisting it lets the UI surface the choice + a restart prompt.
        """
        async with self._session() as session:
            row = await self._get_or_create(session, tenant)
            row.kv_cache_type = value
            await session.commit()

    async def get_agent_max_steps(self, tenant: str) -> int | None:
        """Return the stored agent loop bound (tool rounds per turn), or ``None`` if unset."""
        async with self._session() as session:
            row = await session.get(_LlmPrefRow, tenant)
            return row.agent_max_steps if row is not None else None

    async def set_agent_max_steps(self, tenant: str, value: int | None) -> None:
        """Set or clear the agent loop bound for ``tenant`` (the route clamps the range)."""
        async with self._session() as session:
            row = await self._get_or_create(session, tenant)
            row.agent_max_steps = value
            await session.commit()
