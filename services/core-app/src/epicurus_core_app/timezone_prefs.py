"""Persisted timezone preference (tenant-scoped).

The operator's IANA timezone, stored in the core's Postgres so the agent's ``now`` tool
(and any future time-aware behaviour) resolves a consistent *local* time across restarts
and devices. The table is created by this service's migrations
(:mod:`epicurus_core_app.migrations`), applied at startup (#834); an unset value falls back to
the configured default (``DEFAULT_TIMEZONE``).
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _TzBase(DeclarativeBase):
    pass


class _TimezonePrefRow(_TzBase):
    """One timezone preference per tenant."""

    __tablename__ = "timezone_prefs"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    # IANA timezone name (e.g. "Europe/Belgrade"); NULL means fall back to the configured default.
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TimezonePrefsStore:
    """Read/write the operator's IANA timezone for a tenant."""

    def __init__(self, engine: AsyncEngine, *, default: str = "UTC") -> None:
        self._engine = engine
        self._default = default
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    @property
    def default(self) -> str:
        """The configured fallback timezone used when the tenant has set none."""
        return self._default

    async def init(self) -> None:
        """Build this store's tables from the models — the **unit-test** schema path.

        The deployed service does not call this: its schema comes from the revisions in
        :mod:`epicurus_core_app.migrations`, applied at startup (#834, ADR-XXXX). See that
        module's docstring for why ``create_all`` survives here, and what keeps it honest.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(_TzBase.metadata.create_all)

    async def get_timezone(self, tenant: str) -> str:
        """Return the stored IANA timezone, or the configured default if unset."""
        async with self._session() as session:
            row = await session.get(_TimezonePrefRow, tenant)
            if row is None or not row.timezone:
                return self._default
            return row.timezone

    async def set_timezone(self, tenant: str, timezone: str) -> None:
        """Set the operator's IANA timezone for ``tenant``."""
        async with self._session() as session:
            row = await session.get(_TimezonePrefRow, tenant)
            if row is None:
                row = _TimezonePrefRow(tenant=tenant)
                session.add(row)
            row.timezone = timezone
            await session.commit()
