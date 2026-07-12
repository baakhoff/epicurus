"""Persisted left-nav page order preference (tenant-scoped, #543).

The operator's drag-and-drop order for module-contributed left-nav pages, stored in the
core's Postgres so it syncs across devices instead of living in `localStorage`. Auto-created
on first use via ``PageOrderStore.init()`` — the same pattern as
:class:`~epicurus_core_app.timezone_prefs.TimezonePrefsStore`. The stored list is opaque page
ids (``"<module>/<page_id>"``); merge semantics (unknown ids append, stale ids are ignored)
are a shell/nav concern (ADR-0018) resolved by the web client, not here — this store only
persists whatever ordered list it's given.
"""

from __future__ import annotations

import json

from sqlalchemy import String, Text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns


class _PageOrderBase(DeclarativeBase):
    pass


class _PageOrderRow(_PageOrderBase):
    """One page-order list per tenant."""

    __tablename__ = "page_order_prefs"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    # JSON-encoded list[str] of page ids, most-preferred-first. Empty/NULL means "no
    # preference set" — the caller falls back to the manifest-declared default order.
    order_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class PageOrderStore:
    """Read/write the operator's left-nav page order for a tenant."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_PageOrderBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Reconcile columns added after first release via the shared additive helper (#249)."""
        ensure_columns(sync_conn, _PageOrderRow.__table__, ("order_json",))

    async def get_order(self, tenant: str) -> list[str]:
        """Return the stored page-id order, or `[]` if the tenant has none set."""
        async with self._session() as session:
            row = await session.get(_PageOrderRow, tenant)
            if row is None or not row.order_json:
                return []
            ids: list[str] = json.loads(row.order_json)
            return ids

    async def set_order(self, tenant: str, order: list[str]) -> None:
        """Persist `order` (page ids, most-preferred-first) for `tenant`, replacing any prior."""
        async with self._session() as session:
            row = await session.get(_PageOrderRow, tenant)
            if row is None:
                row = _PageOrderRow(tenant=tenant)
                session.add(row)
            row.order_json = json.dumps(order)
            await session.commit()
