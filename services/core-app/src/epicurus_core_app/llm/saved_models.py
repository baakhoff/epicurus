"""Persisted saved hosted-model ids (tenant-scoped).

The hosted / API model ids the operator has actually used (e.g. ``claude/<model-id>``),
stored in the core's Postgres so they survive restarts, survive a PWA reinstall, and follow
the tenant across devices and origins — unlike the web client's ``recentModels``
localStorage cache, which is per-device, per-origin, and capped at five (#496).

Model ids are the caller's choice, not code (ADR-0010); this table gives the ids the
operator picks a durable home so they become first-class rows: offered in the chat picker
on any device, listed on the Models page (removable, settable as the global default), and
assignable to a module's model slot (ADR-0029).

Local ids never belong here — the route validates each id as *hosted* (a known
provider-alias prefix) via :func:`epicurus_core_app.llm.providers.is_hosted`, so a local
``hf.co/org/model:tag`` can never masquerade as a hosted entry. Auto-created on first use
via ``SavedHostedModelStore.init()`` (the same pattern as ``LlmPrefsStore``).
"""

from __future__ import annotations

import time

from sqlalchemy import BigInteger, String, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns


def _now_ms() -> int:
    """Epoch milliseconds — the save timestamp. A module-level seam tests monkeypatch to
    make ordering deterministic without a real clock."""
    return int(time.time() * 1000)


class _SavedBase(DeclarativeBase):
    pass


class _SavedModelRow(_SavedBase):
    """One saved hosted-model id, scoped to ``(tenant, model)``."""

    __tablename__ = "saved_models"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    # The hosted model id exactly as the operator entered it, e.g.
    # "claude/claude-3-5-sonnet-latest".
    model: Mapped[str] = mapped_column(String(256), primary_key=True)
    # Epoch milliseconds of the most recent save — drives most-recent-first ordering and is
    # bumped when an existing id is re-saved. BigInteger, not Integer: epoch-ms (~1.7e12)
    # overflows Postgres INTEGER (int32), the same class of bug as the *_ns columns (#249), so
    # BigInteger is the safe default for any epoch column even though SQLite tolerates the width.
    added_at: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class SavedHostedModelStore:
    """Read/write the tenant's saved hosted-model ids (#496)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_SavedBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Reconcile columns added after first release via the shared additive helper (#249).

        A ``saved_models`` table provisioned before ``added_at`` existed self-heals rather than
        500ing on every read. See :func:`epicurus_core.db.ensure_columns`.
        """
        ensure_columns(sync_conn, _SavedModelRow.__table__, ("added_at",))

    async def list(self, tenant: str) -> list[str]:
        """The tenant's saved hosted-model ids, most-recently-saved first."""
        async with self._session() as session:
            rows = await session.execute(
                select(_SavedModelRow.model)
                .where(_SavedModelRow.tenant == tenant)
                .order_by(_SavedModelRow.added_at.desc(), _SavedModelRow.model.asc())
            )
            return list(rows.scalars())

    async def add(self, tenant: str, model: str) -> None:
        """Save ``model`` for ``tenant`` (idempotent; a re-save bumps it to the front).

        A single atomic ``INSERT … ON CONFLICT DO UPDATE`` rather than get-then-insert, so two
        concurrent first-saves of the same id can't race in the gap between the read and the
        write to a composite-PK ``IntegrityError`` (a 500). Effectively unreachable for a single
        operator, but the upsert keeps it correct under concurrency (#537). Dialect-specific
        because ``ON CONFLICT`` is not in core SQLAlchemy — Postgres in production, SQLite in tests.
        """
        now_ms = _now_ms()
        insert = pg_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        stmt = (
            insert(_SavedModelRow)
            .values(tenant=tenant, model=model, added_at=now_ms)
            .on_conflict_do_update(index_elements=["tenant", "model"], set_={"added_at": now_ms})
        )
        async with self._session() as session:
            await session.execute(stmt)
            await session.commit()

    async def remove(self, tenant: str, model: str) -> None:
        """Forget a saved hosted model for ``tenant`` (a no-op if it wasn't saved)."""
        async with self._session() as session:
            await session.execute(
                delete(_SavedModelRow).where(
                    _SavedModelRow.tenant == tenant, _SavedModelRow.model == model
                )
            )
            await session.commit()
