"""Persisted per-module preferences — the enable/disable flag (tenant-scoped).

Stored in the core's Postgres database so the operator's choice survives restarts and
is consistent across devices. Disabling a module hides its tools, pages, and actions
from the agent and the shell while the **container keeps running** (issue #126) — the
flag lives here in the core, never in the module. Auto-created on first use via
``init`` (same pattern as ``LlmPrefsStore`` / ``ConversationStore``).
"""

from __future__ import annotations

from sqlalchemy import Boolean, String, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _ModulePrefBase(DeclarativeBase):
    pass


class _ModulePrefRow(_ModulePrefBase):
    """One row per ``(tenant, module)`` — the operator's per-module preferences."""

    __tablename__ = "module_prefs"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    module: Mapped[str] = mapped_column(String(128), primary_key=True)
    # False hides the module from the agent + shell; the container keeps running (#126).
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ModulePrefsStore:
    """Read/write per-module operator preferences for a tenant."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_ModulePrefBase.metadata.create_all)

    async def enabled_map(self, tenant: str) -> dict[str, bool]:
        """Every stored enabled flag for ``tenant``.

        A module with no row is enabled by default, so callers treat a missing key
        as ``True`` — the map only carries flags the operator has explicitly set.
        """
        async with self._session() as session:
            rows = await session.scalars(
                select(_ModulePrefRow).where(_ModulePrefRow.tenant == tenant)
            )
            return {row.module: row.enabled for row in rows}

    async def is_enabled(self, tenant: str, module: str) -> bool:
        """Whether ``module`` is enabled for ``tenant`` (default ``True`` when unset)."""
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            return True if row is None else row.enabled

    async def set_enabled(self, tenant: str, module: str, enabled: bool) -> None:
        """Persist the enable/disable flag for ``module`` (upsert)."""
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            if row is None:
                session.add(_ModulePrefRow(tenant=tenant, module=module, enabled=enabled))
            else:
                row.enabled = enabled
            await session.commit()
