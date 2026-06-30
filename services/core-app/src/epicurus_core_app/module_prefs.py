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

from sqlalchemy import Boolean, String, Text, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import CollectionPrefs
from epicurus_core.db import ensure_columns


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
    # JSON list of tool names the operator has explicitly disabled (#213). An absent tool
    # (not in the list) is enabled by default; the agent never receives a listed tool.
    disabled_tools: Mapped[str] = mapped_column(Text, default="[]", server_default="'[]'")
    # JSON ``CollectionPrefs`` — the operator's enabled collections + active view for an
    # account/collection module (calendar, tasks) (ADR-0030). Empty (``{}``) means "use the
    # silent local default": no enabled external collection, no active view.
    collections: Mapped[str] = mapped_column(Text, default="{}", server_default="'{}'")
    # Whether agent-proposed changes go through review (#KB-refactor). Default on (NULL on a
    # pre-existing row ⇒ on). When off, a module that supports suggestions applies the
    # agent's change directly instead of staging it for the operator.
    suggestions_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


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
        """Reconcile columns added after first release via the shared additive helper (#249).

        ``removed`` (#127), per-slot ``models`` (#128), ``disabled_tools`` (#213),
        ``collections`` (ADR-0030), and ``suggestions_enabled`` (#KB-refactor) all postdate
        the table's first release. The JSON columns carry a ``server_default`` so the helper
        backfills them; the booleans (no server default) are added nullable. See
        :func:`epicurus_core.db.ensure_columns`.
        """
        ensure_columns(
            sync_conn,
            _ModulePrefRow.__table__,
            ("removed", "models", "disabled_tools", "collections", "suggestions_enabled"),
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

    async def get_collections(self, tenant: str, module: str) -> CollectionPrefs:
        """The operator's collection selection for ``module`` (ADR-0030).

        Returns empty prefs (``enabled=[]``, ``active=None`` — "use the local default")
        when the module has no stored selection.
        """
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            if row is None:
                return CollectionPrefs()
            return CollectionPrefs.model_validate_json(row.collections or "{}")

    async def set_collections(self, tenant: str, module: str, prefs: CollectionPrefs) -> None:
        """Replace ``module``'s collection selection (upsert) (ADR-0030)."""
        encoded = prefs.model_dump_json()
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            if row is None:
                session.add(_ModulePrefRow(tenant=tenant, module=module, collections=encoded))
            else:
                row.collections = encoded
            await session.commit()

    async def get_suggestions_enabled(self, tenant: str, module: str) -> bool:
        """Whether agent changes to ``module`` go through review (default ``True``) (#KB-refactor).

        A missing row or a NULL column (pre-existing deployment) both mean "on" — review is
        the safe default; the operator turns it off to auto-accept the agent's changes.
        """
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            if row is None or row.suggestions_enabled is None:
                return True
            return row.suggestions_enabled

    async def set_suggestions_enabled(self, tenant: str, module: str, enabled: bool) -> None:
        """Persist whether ``module``'s agent changes go through review (upsert) (#KB-refactor)."""
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            if row is None:
                session.add(
                    _ModulePrefRow(tenant=tenant, module=module, suggestions_enabled=enabled)
                )
            else:
                row.suggestions_enabled = enabled
            await session.commit()

    async def get_disabled_tools(self, tenant: str, module: str) -> set[str]:
        """The set of tool names explicitly disabled for ``module`` (#213).

        An absent tool (not in the set) is enabled by default; the set only carries
        names the operator has explicitly disabled.
        """
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            if row is None:
                return set()
            return set(cast("list[str]", json.loads(row.disabled_tools or "[]")))

    async def set_tool_enabled(self, tenant: str, module: str, tool: str, enabled: bool) -> None:
        """Enable or disable a single tool for ``module`` (upsert, set-based) (#213).

        Disabled tool names are stored in a JSON list; enabling removes a name from the
        list, disabling adds it. The list only ever contains explicitly disabled tools —
        an absent name is implicitly enabled.
        """
        async with self._session() as session:
            row = await session.get(_ModulePrefRow, (tenant, module))
            if row is None:
                disabled: set[str] = set()
                row = _ModulePrefRow(tenant=tenant, module=module)
                session.add(row)
            else:
                disabled = set(cast("list[str]", json.loads(row.disabled_tools or "[]")))
            if enabled:
                disabled.discard(tool)
            else:
                disabled.add(tool)
            row.disabled_tools = json.dumps(sorted(disabled))
            await session.commit()
