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

Each row may also carry a **capability override** (#711) — the operator's correction to what
the core *believes* about the model when LiteLLM's static cost map is wrong or silent. See
:class:`SavedModelOverride`.
"""

from __future__ import annotations

import time
from typing import Any, Literal, cast

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Integer, String, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns

# How a saved model's vision capability is decided: trust the map, or force it either way.
VisionOverride = Literal["auto", "on", "off"]


class SavedModelOverride(BaseModel):
    """The operator's correction to a saved hosted model's *declared* capabilities (#711).

    This is **metadata**, not tuning. ``ModelSettings`` answers "how much context should this
    model *use*" (``num_ctx``, a runtime knob); this answers "what is this model *capable* of"
    — which the core otherwise takes from LiteLLM's static cost map. That map is missing
    entries entirely (``xai/grok-latest``) and mislabels others, and the consequence is real:
    a vision-capable model resolves to ``supports_vision() is False`` and the image gate (#633)
    refuses image turns for a model that would have handled them.

    Defaults are the pre-override behaviour exactly — ``auto`` vision and no context length
    mean "ask the map", so an absent or empty override changes nothing.
    """

    vision: VisionOverride = "auto"
    # The model's *declared* context length, for badges and as the ceiling on the
    # context-window suggestion. None = take the map's answer. Not the operator's chosen
    # num_ctx — that is ``ModelSettings.context_window``, a different layer.
    context_length: int | None = Field(default=None, gt=0)

    def is_empty(self) -> bool:
        """True when the override says nothing the map doesn't already say."""
        return self.vision == "auto" and self.context_length is None


def _now_ms() -> int:
    """Epoch milliseconds — the save timestamp. A module-level seam tests monkeypatch to
    make ordering deterministic without a real clock."""
    return int(time.time() * 1000)


def _to_override(vision: str | None, context_length: int | None) -> SavedModelOverride:
    """Build an override from its two stored columns, tolerating a stale/unknown value.

    A ``vision_override`` outside the vocabulary (hand-edited SQL, or a value written by a
    newer build) degrades to ``auto`` rather than raising: a capability *hint* must never be
    able to break the model list it decorates.
    """
    if vision == "on":
        return SavedModelOverride(vision="on", context_length=context_length)
    if vision == "off":
        return SavedModelOverride(vision="off", context_length=context_length)
    return SavedModelOverride(context_length=context_length)


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
    # The capability override (#711), stored flat. NULL on both means "no override" — the
    # absence *is* the "auto" case, so an untouched row keeps the map's answers verbatim.
    vision_override: Mapped[str | None] = mapped_column(String(8), nullable=True)
    context_length_override: Mapped[int | None] = mapped_column(Integer, nullable=True)


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

        A ``saved_models`` table provisioned before ``added_at`` (or before the #711 override
        columns) existed self-heals rather than 500ing on every read. See
        :func:`epicurus_core.db.ensure_columns`.
        """
        ensure_columns(
            sync_conn,
            _SavedModelRow.__table__,
            ("added_at", "vision_override", "context_length_override"),
        )

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

    async def overrides(self, tenant: str) -> dict[str, SavedModelOverride]:
        """Every saved model's capability override for ``tenant``, keyed by model id (#711).

        Only rows that actually carry one appear — the list route and the gateway both treat a
        missing key as :class:`SavedModelOverride`'s defaults, i.e. today's map-driven answers.
        """
        async with self._session() as session:
            rows = await session.execute(
                select(
                    _SavedModelRow.model,
                    _SavedModelRow.vision_override,
                    _SavedModelRow.context_length_override,
                ).where(_SavedModelRow.tenant == tenant)
            )
            out: dict[str, SavedModelOverride] = {}
            for model, vision, context_length in rows:
                override = _to_override(vision, context_length)
                if not override.is_empty():
                    out[model] = override
            return out

    async def get_override(self, tenant: str, model: str) -> SavedModelOverride:
        """One saved model's capability override — all-defaults when unsaved or unset (#711)."""
        async with self._session() as session:
            row = (
                await session.execute(
                    select(
                        _SavedModelRow.vision_override,
                        _SavedModelRow.context_length_override,
                    ).where(_SavedModelRow.tenant == tenant, _SavedModelRow.model == model)
                )
            ).first()
        return _to_override(row[0], row[1]) if row is not None else SavedModelOverride()

    async def set_override(self, tenant: str, model: str, override: SavedModelOverride) -> bool:
        """Store ``model``'s capability override; False when the model isn't saved (#711).

        An override is a property *of a saved row*, so this updates rather than upserts — the
        caller 404s an id the tenant hasn't saved instead of silently creating a row that the
        saved-model list would then surface. An empty override clears both columns back to
        NULL, which is exactly the pre-override state.
        """
        async with self._session() as session:
            result = await session.execute(
                update(_SavedModelRow)
                .where(_SavedModelRow.tenant == tenant, _SavedModelRow.model == model)
                .values(
                    vision_override=None if override.vision == "auto" else override.vision,
                    context_length_override=override.context_length,
                )
            )
            await session.commit()
        # A DML execute yields a CursorResult at runtime; the async signature is the wider
        # Result, which doesn't declare ``rowcount``. One UPDATE (not SELECT-then-UPDATE) so
        # "does this row exist" and "write it" can't disagree under concurrency.
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def remove(self, tenant: str, model: str) -> None:
        """Forget a saved hosted model for ``tenant`` (a no-op if it wasn't saved)."""
        async with self._session() as session:
            await session.execute(
                delete(_SavedModelRow).where(
                    _SavedModelRow.tenant == tenant, _SavedModelRow.model == model
                )
            )
            await session.commit()
