"""Persisted lead-time preference for `calendar.event_starting_soon` (tenant-scoped, #664).

Calendar owns its own Postgres (unlike `timezone_prefs`/`page_order_prefs`, which live in
core-app's database) — so a calendar-specific tenant setting needs its own store rather than
reusing core-app's, following the same settings-primitives shape (a tiny tenant-keyed table, a
store, a default) documented for `timezone_prefs`/`page_order_prefs`/`maintenance_schedule_prefs`
(ADR-0098 §2). No HTTP route ships in this PR — #664 is about the events themselves, not a
settings UI; the default (15 minutes) applies until an operator-facing control exists.
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_LEAD_MINUTES = 15
"""Default lead time for `calendar.event_starting_soon` when the tenant has set none."""


class _LeadTimeBase(DeclarativeBase):
    pass


class _LeadTimePrefRow(_LeadTimeBase):
    """One lead-time preference per tenant."""

    __tablename__ = "calendar_lead_time_prefs"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    # Minutes before an event's start that `event_starting_soon` fires. NULL means fall back
    # to DEFAULT_LEAD_MINUTES.
    lead_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)


class LeadTimePrefsStore:
    """Read/write the operator's `event_starting_soon` lead time for a tenant (#664)."""

    def __init__(self, engine: AsyncEngine, *, default: int = DEFAULT_LEAD_MINUTES) -> None:
        self._engine = engine
        self._default = default
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    @property
    def default(self) -> int:
        """The configured fallback lead time (minutes) used when the tenant has set none."""
        return self._default

    async def init(self) -> None:
        """Build this store's table straight from the models — the **unit-test** schema path.

        The deployed service does not call this; its schema comes from the migration
        environment in :mod:`epicurus_calendar.migrations`, applied once at startup by
        :func:`epicurus_core.db.migrations.run_migrations` (#834, #928, ADR-XXXX). It survives
        for the tests, where a fresh SQLite file per test is cheaper to build from the models
        than to migrate.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(_LeadTimeBase.metadata.create_all)

    async def get_lead_minutes(self, tenant: str) -> int:
        """Return the stored lead time (minutes), or the configured default if unset."""
        async with self._session() as session:
            row = await session.get(_LeadTimePrefRow, tenant)
            if row is None or row.lead_minutes is None:
                return self._default
            return row.lead_minutes

    async def set_lead_minutes(self, tenant: str, lead_minutes: int) -> None:
        """Set the operator's `event_starting_soon` lead time (minutes) for `tenant`."""
        async with self._session() as session:
            row = await session.get(_LeadTimePrefRow, tenant)
            if row is None:
                row = _LeadTimePrefRow(tenant=tenant)
                session.add(row)
            row.lead_minutes = lead_minutes
            await session.commit()
