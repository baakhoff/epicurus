"""Persisted lead-time preference for `tasks.task_due_soon` (tenant-scoped, #664).

Tasks owns its own Postgres (unlike `timezone_prefs`/`page_order_prefs`, which live in
core-app's database) — so a tasks-specific tenant setting needs its own store rather than
reusing core-app's, following the same settings-primitives shape (a tiny tenant-keyed table, a
store, a default) documented for `timezone_prefs`/`page_order_prefs`/`maintenance_schedule_prefs`
(ADR-0098 §2) — the same shape calendar's own `lead_time_prefs.py` uses, in days rather than
minutes since a task's `due` is date-granular, not an instant. No HTTP route ships in this PR
— #664 is about the events themselves, not a settings UI; the default (1 day) applies until an
operator-facing control exists.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_LEAD_DAYS = 1
"""Default lead time for `tasks.task_due_soon` when the tenant has set none."""


class _LeadTimeBase(DeclarativeBase):
    pass


class _LeadTimePrefRow(_LeadTimeBase):
    """One lead-time preference per tenant."""

    __tablename__ = "tasks_lead_time_prefs"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    # Days before a task's due date that `task_due_soon` fires. NULL means fall back to
    # DEFAULT_LEAD_DAYS.
    lead_days: Mapped[int | None] = mapped_column(Integer, nullable=True)


class LeadTimePrefsStore:
    """Read/write the operator's `task_due_soon` lead time for a tenant (#664)."""

    def __init__(self, engine: AsyncEngine, *, default: int = DEFAULT_LEAD_DAYS) -> None:
        self._engine = engine
        self._default = default
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    @property
    def default(self) -> int:
        """The configured fallback lead time (days) used when the tenant has set none."""
        return self._default

    async def init(self) -> None:
        """Build this store's table straight from the model — the **unit-test** schema path.

        The deployed service does not call this; its schema comes from the migration
        environment (#929, ADR-XXXX). It survives for the tests, where a fresh SQLite file per
        test is cheaper to build from the model than to migrate. Honest only because the
        `migrations` CI gate proves the model and the revisions agree on real Postgres.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(_LeadTimeBase.metadata.create_all)

    async def get_lead_days(self, tenant: str) -> int:
        """Return the stored lead time (days), or the configured default if unset."""
        async with self._session() as session:
            row = await session.get(_LeadTimePrefRow, tenant)
            if row is None or row.lead_days is None:
                return self._default
            return row.lead_days

    async def set_lead_days(self, tenant: str, lead_days: int) -> None:
        """Set the operator's `task_due_soon` lead time (days) for `tenant`."""
        async with self._session() as session:
            row = await session.get(_LeadTimePrefRow, tenant)
            if row is None:
                row = _LeadTimePrefRow(tenant=tenant)
                session.add(row)
            row.lead_days = lead_days
            await session.commit()

    async def export_pref(self, tenant: str) -> int | None:
        """The explicitly-set lead time for *tenant*, or ``None`` if unset (portability export,
        #871). An unset preference has nothing worth carrying — the default travels with the
        module's code, not the archive."""
        async with self._session() as session:
            row = await session.get(_LeadTimePrefRow, tenant)
            if row is None or row.lead_days is None:
                return None
            return row.lead_days

    async def upsert(
        self, tenant: str, lead_days: int, *, dry_run: bool = False
    ) -> Literal["created", "updated"]:
        """Upsert the lead-time preference for *tenant* (portability import, #871).

        *dry_run* reports the outcome without writing.
        """
        async with self._session() as session:
            row = await session.get(_LeadTimePrefRow, tenant)
            outcome: Literal["created", "updated"] = "updated" if row is not None else "created"
            if dry_run:
                return outcome
            if row is None:
                row = _LeadTimePrefRow(tenant=tenant)
                session.add(row)
            row.lead_days = lead_days
            await session.commit()
        return outcome
