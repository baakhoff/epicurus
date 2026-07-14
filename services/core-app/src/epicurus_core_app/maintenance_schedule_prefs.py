"""The maintenance orchestrator's schedule — enable/disable, cadence, time of day (#621).

The orchestrator (``maintenance.py``, ADR-0060) originally woke at one fixed, env-configured
hour, opt-in only. This generalizes the *trigger* into a real, per-tenant, runtime-editable
schedule — enable/disable, an ``hourly``/``daily``/``weekly`` cadence, an hour (0-23), and a
weekday (weekly only) — following the same settings-primitives shape as ``timezone_prefs`` /
``page_order_prefs``: a tiny tenant-keyed table, a store, read via a zero-arg async provider
(the same pattern ``timezone: TimezoneProvider`` already uses). The job registry itself
(``maintenance.py``'s ``list[MaintenanceJob]``) is untouched — this module only generalizes
*when* the nightly batch fires, not what it runs.

A missing row (no tenant has ever changed the schedule) falls back to the env-configured
defaults (``MAINTENANCE_SCHEDULE_ENABLED``/``MAINTENANCE_HOUR``) with ``cadence="daily"`` — a
fresh install behaves exactly as it did before this existed. Once a tenant sets a schedule via
``PUT``, the row is authoritative for every field (no partial/inherit-per-field semantics —
simpler, and a schedule is one cohesive unit, not independently overridable knobs).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns

CADENCES = ("hourly", "daily", "weekly")


@dataclass(frozen=True)
class MaintenanceSchedule:
    """The effective schedule for one tenant's nightly maintenance batch."""

    enabled: bool
    cadence: str  # "hourly" | "daily" | "weekly"
    hour: int  # 0-23; ignored for "hourly"
    weekday: int | None = None  # 0=Monday..6=Sunday; set only when cadence == "weekly"


def validate_cadence(cadence: str, hour: int, weekday: int | None) -> None:
    """Raise ``ValueError`` if *cadence*/*hour*/*weekday* don't form a valid schedule."""
    if cadence not in CADENCES:
        raise ValueError(f"cadence must be one of {CADENCES}, got {cadence!r}")
    if not (0 <= hour <= 23):
        raise ValueError("hour must be 0-23")
    if cadence == "weekly":
        if weekday is None or not (0 <= weekday <= 6):
            raise ValueError("weekday (0=Monday..6=Sunday) is required for a weekly cadence")
    elif weekday is not None:
        raise ValueError("weekday only applies to a weekly cadence")


def next_run_at(schedule: MaintenanceSchedule, now: datetime) -> datetime:
    """The next local wall-clock time (same tzinfo as *now*) this schedule is due to fire.

    A display estimate for the UI's "next planned run" — the scheduler's own due-check
    (:func:`is_due`) is what actually gates firing and additionally avoids re-firing within
    an already-handled window.
    """
    if schedule.cadence == "hourly":
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    target = now.replace(hour=schedule.hour, minute=0, second=0, microsecond=0)
    if schedule.cadence == "daily":
        if target <= now:
            target += timedelta(days=1)
        return target
    # weekly
    assert schedule.weekday is not None
    days_ahead = (schedule.weekday - now.weekday()) % 7
    if days_ahead == 0 and target <= now:
        days_ahead = 7
    return target + timedelta(days=days_ahead)


def is_due(schedule: MaintenanceSchedule, local_now: datetime, last_fired: datetime | None) -> bool:
    """Whether the batch should fire now, and hasn't already fired in this exact window.

    ``last_fired`` (the orchestrator's own in-memory bookkeeping — never persisted, see the
    module docstring) is compared by local calendar date (and, for ``hourly``, local hour
    too), so a poll tick landing anywhere inside the target window fires exactly once, not
    once per poll interval — mirroring ``scheduled_turns._is_due``.
    """
    if not schedule.enabled:
        return False
    if schedule.cadence == "hourly":
        if last_fired is not None:
            last_local = last_fired.astimezone(local_now.tzinfo)
            if (last_local.date(), last_local.hour) == (local_now.date(), local_now.hour):
                return False
        return True
    if local_now.hour != schedule.hour:
        return False
    if schedule.cadence == "weekly" and local_now.weekday() != schedule.weekday:
        return False
    if last_fired is not None:
        last_local = last_fired.astimezone(local_now.tzinfo)
        if last_local.date() == local_now.date():
            return False
    return True


class _Base(DeclarativeBase):
    pass


class _MaintenanceScheduleRow(_Base):
    """One tenant's maintenance schedule override; a missing row means "use the env default"."""

    __tablename__ = "maintenance_schedule_prefs"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    cadence: Mapped[str] = mapped_column(String(16))
    hour: Mapped[int] = mapped_column(Integer)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MaintenanceScheduleStore:
    """Read/write a tenant's maintenance schedule override, falling back to the env default."""

    def __init__(self, engine: AsyncEngine, *, default_enabled: bool, default_hour: int) -> None:
        self._engine = engine
        self._default = MaintenanceSchedule(
            enabled=default_enabled, cadence="daily", hour=default_hour % 24, weekday=None
        )
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    @property
    def default(self) -> MaintenanceSchedule:
        """The env-configured fallback used when the tenant has never set a schedule."""
        return self._default

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        ensure_columns(
            sync_conn, _MaintenanceScheduleRow.__table__, ("enabled", "cadence", "hour", "weekday")
        )

    async def get(self, tenant: str) -> MaintenanceSchedule:
        """The tenant's schedule, or the env-configured default if it has never set one."""
        async with self._session() as session:
            row = await session.get(_MaintenanceScheduleRow, tenant)
            if row is None:
                return self._default
            return MaintenanceSchedule(
                enabled=row.enabled, cadence=row.cadence, hour=row.hour, weekday=row.weekday
            )

    async def set(self, tenant: str, schedule: MaintenanceSchedule) -> MaintenanceSchedule:
        """Set the tenant's schedule (validated by the caller before this is reached)."""
        async with self._session() as session:
            row = await session.get(_MaintenanceScheduleRow, tenant)
            if row is None:
                row = _MaintenanceScheduleRow(tenant=tenant)
                session.add(row)
            row.enabled = schedule.enabled
            row.cadence = schedule.cadence
            row.hour = schedule.hour % 24
            row.weekday = schedule.weekday
            await session.commit()
            return schedule


__all__ = [
    "CADENCES",
    "MaintenanceSchedule",
    "MaintenanceScheduleStore",
    "is_due",
    "next_run_at",
    "validate_cadence",
]
