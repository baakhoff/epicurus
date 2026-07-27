"""Per-event alert subscriptions (#732) — "push me when X happens" with no automation.

A tenant-scoped ``(tenant, module, event_type) -> ChannelPrefs`` table: an operator opts in
to exactly the module events they want announced, individually, without configuring an
automation. Unlike :mod:`epicurus_core_app.push.prefs` (whose ``categories`` default to
``{push: true, center: true}`` — off by exception), a subscription's default is **no row**,
which :meth:`EventSubscriptionStore.get` returns as ``None`` — not the ``ChannelPrefs()``
default. Everything is off until the operator turns a specific event on; the settings UI
renders the full declared-event catalog (every module's ``events_emitted``) and this store
only ever holds the sparse, non-default rows layered on top of it — the same "known list
plus sparse overrides" shape ``PushPrefsStore.categories`` uses, just with the opposite
default.

:meth:`set` upserts a row when either channel is on, and deletes it when both are off — so
"no row" and "explicitly both off" collapse to the same state, and the table never
accumulates all-false rows for events an operator glanced at and left alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Boolean, String, UniqueConstraint, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core_app.push.prefs import ChannelPrefs

__all__ = ["EventSubscription", "EventSubscriptionStore"]


@dataclass(frozen=True)
class EventSubscription:
    """One stored ``(module, event_type) -> ChannelPrefs`` row for a tenant."""

    module: str
    event_type: str
    push: bool
    center: bool


class _Base(DeclarativeBase):
    pass


class _EventSubscriptionRow(_Base):
    """ORM mapping — one row per subscribed ``(tenant, module, event_type)``."""

    __tablename__ = "event_subscriptions"
    __table_args__ = (
        UniqueConstraint("tenant", "module", "event_type", name="uq_event_subscriptions_key"),
    )

    pk: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    module: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(128))
    push: Mapped[bool] = mapped_column(Boolean, default=True)
    center: Mapped[bool] = mapped_column(Boolean, default=True)


def _to_value(row: _EventSubscriptionRow) -> EventSubscription:
    return EventSubscription(
        module=row.module, event_type=row.event_type, push=row.push, center=row.center
    )


class EventSubscriptionStore:
    """Read/write a tenant's per-event alert subscriptions."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema (idempotent).

        No ``ensure_columns`` call: this table is new in this release, so it has no
        deployed predecessor to reconcile against (the same reasoning ``EventLogStore.
        init`` documents) — the first column added *after* this ships must add one
        (ADR-0067).
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def list(self, tenant: str) -> list[EventSubscription]:
        """Every non-default subscription for *tenant* — a sparse overlay, not a full catalog.

        The full declared-event catalog (every module's ``events_emitted``) lives in each
        module's manifest, not here; the settings UI unions that with this list client-side,
        the same way ``PushNotificationsCard`` unions ``known_categories`` with the tenant's
        sparse ``categories`` overrides.
        """
        async with self._session() as session:
            rows = await session.scalars(
                select(_EventSubscriptionRow)
                .where(_EventSubscriptionRow.tenant == tenant)
                .order_by(_EventSubscriptionRow.module, _EventSubscriptionRow.event_type)
            )
            return [_to_value(r) for r in rows]

    async def get(self, tenant: str, *, module: str, event_type: str) -> ChannelPrefs | None:
        """The stored prefs for one event, or ``None`` if the operator never subscribed."""
        async with self._session() as session:
            row = await session.scalar(
                select(_EventSubscriptionRow).where(
                    _EventSubscriptionRow.tenant == tenant,
                    _EventSubscriptionRow.module == module,
                    _EventSubscriptionRow.event_type == event_type,
                )
            )
            if row is None:
                return None
            return ChannelPrefs(push=row.push, center=row.center)

    async def set(self, tenant: str, *, module: str, event_type: str, prefs: ChannelPrefs) -> None:
        """Upsert the row, or delete it when *prefs* turns both channels off.

        A single atomic ``INSERT ... ON CONFLICT DO UPDATE`` rather than get-then-write, so
        two toggles for the same event racing (two browser tabs, a double-click) land as
        "last write wins" on the database's own atomicity rather than on whichever request's
        read happened to go first (the same reasoning ``SavedModelStore.add`` documents).
        Dialect-specific because ``ON CONFLICT`` is not in core SQLAlchemy — Postgres in
        production, SQLite in tests.
        """
        if not prefs.push and not prefs.center:
            async with self._session() as session:
                await session.execute(
                    delete(_EventSubscriptionRow).where(
                        _EventSubscriptionRow.tenant == tenant,
                        _EventSubscriptionRow.module == module,
                        _EventSubscriptionRow.event_type == event_type,
                    )
                )
                await session.commit()
            return
        insert = pg_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        stmt = (
            insert(_EventSubscriptionRow)
            .values(
                tenant=tenant,
                module=module,
                event_type=event_type,
                push=prefs.push,
                center=prefs.center,
            )
            .on_conflict_do_update(
                index_elements=["tenant", "module", "event_type"],
                set_={"push": prefs.push, "center": prefs.center},
            )
        )
        async with self._session() as session:
            await session.execute(stmt)
            await session.commit()
