"""Persisted per-module preferences — the enable/disable flag (tenant-scoped).

Stored in the core's Postgres database so the operator's choice survives restarts and
is consistent across devices. Disabling a module hides its tools, pages, and actions
from the agent and the shell while the **container keeps running** (issue #126) — the
flag lives here in the core, never in the module. Auto-created on first use via
``init`` (same pattern as ``LlmPrefsStore`` / ``ConversationStore``).
"""

from __future__ import annotations

import json
from typing import cast

from sqlalchemy import Boolean, String, Text, inspect, select
from sqlalchemy.engine import Connection
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
    # True tombstones the module after its container is removed (#127): it is hidden from
    # every surface, and re-removed on startup if a reconcile has resurrected the container.
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    # JSON ``{slot_key: model_id}`` — the operator's per-slot model choices (#128). A slot
    # absent here falls back to the core default model.
    models: Mapped[str] = mapped_column(Text, default="{}", server_default="'{}'")


class ModulePrefsStore:
    """Read/write per-module operator preferences for a tenant."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_ModulePrefBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Idempotently add columns introduced after the table's first release.

        No migration framework (the store uses ``create_all``); on a deployment that
        predates the ``removed`` column (#127) we add it in place, with a per-dialect type
        so it is portable across Postgres and the tests' SQLite.
        """
        inspector = inspect(sync_conn)
        existing = {col["name"] for col in inspector.get_columns(_ModulePrefRow.__tablename__)}
        for name in ("removed", "models"):
            if name not in existing:
                type_sql = _ModulePrefRow.__table__.c[name].type.compile(dialect=sync_conn.dialect)
                sync_conn.exec_driver_sql(
                    f"ALTER TABLE {_ModulePrefRow.__tablename__} ADD COLUMN {name} {type_sql}"
                )

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

    async def removed_modules(self, tenant: str) -> set[str]:
        """The set of modules tombstoned (container removed) for ``tenant`` (#127)."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_ModulePrefRow.module).where(
                    _ModulePrefRow.tenant == tenant,
                    _ModulePrefRow.removed.is_(True),
                )
            )
            return set(rows)

    async def set_removed(self, tenant: str, module: str, removed: bool) -> None:
        """Tombstone (or clear the tombstone for) ``module`` after container removal (#127)."""
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            if row is None:
                session.add(_ModulePrefRow(tenant=tenant, module=module, removed=removed))
            else:
                row.removed = removed
            await session.commit()

    async def get_models(self, tenant: str, module: str) -> dict[str, str]:
        """The operator's per-slot model choices for ``module`` (``{}`` when unset) (#128)."""
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            if row is None:
                return {}
            return cast("dict[str, str]", json.loads(row.models or "{}"))

    async def set_models(self, tenant: str, module: str, models: dict[str, str]) -> None:
        """Replace ``module``'s per-slot model choices (upsert) (#128)."""
        encoded = json.dumps(models)
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            if row is None:
                session.add(_ModulePrefRow(tenant=tenant, module=module, models=encoded))
            else:
                row.models = encoded
            await session.commit()
