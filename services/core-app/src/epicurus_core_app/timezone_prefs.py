"""Persisted timezone preference (tenant-scoped).

The operator's IANA timezone, stored in the core's Postgres so the agent's ``now`` tool
(and any future time-aware behaviour) resolves a consistent *local* time across restarts
and devices. Auto-created on first use via ``TimezonePrefsStore.init()`` — the same pattern
as :class:`~epicurus_core_app.llm.prefs.LlmPrefsStore`; an unset value falls back to the
configured default (``DEFAULT_TIMEZONE``).
"""

from __future__ import annotations

from sqlalchemy import String, inspect
from sqlalchemy.engine import Connection
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
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_TzBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Idempotently add columns introduced after the table's first release.

        No migration framework (the store uses ``create_all``); mirrors
        ``LlmPrefsStore._ensure_columns`` so a pre-existing table self-heals rather than
        500ing on a column added in a later release.
        """
        inspector = inspect(sync_conn)
        existing = {col["name"] for col in inspector.get_columns(_TimezonePrefRow.__tablename__)}
        table = _TimezonePrefRow.__tablename__
        for name in ("timezone",):
            if name not in existing:
                col = _TimezonePrefRow.__table__.c[name]
                type_sql = col.type.compile(dialect=sync_conn.dialect)
                sync_conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {type_sql}")

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
