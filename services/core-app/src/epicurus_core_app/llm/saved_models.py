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
``hf.co/org/model:tag`` can never masquerade as a hosted entry. The table is created by this
service's migrations (:mod:`epicurus_core_app.migrations`), applied at startup (#834).

Each row may also carry a **capability override** (#711) — the operator's correction to what
the core *believes* about the model when LiteLLM's static cost map is wrong or silent. See
:class:`SavedModelOverride`.
"""

from __future__ import annotations

import time
from typing import Any, Literal, cast

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Integer, String, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# How a saved model's vision capability is decided: trust the map, or force it either way.
VisionOverride = Literal["auto", "on", "off"]
# The same vocabulary for tool calling (#947). Deliberately *not* a new
# ``supported|unsupported|unknown`` spelling: one vocabulary for every capability keeps the
# store, the route, the sheet and the documented resolution order extending rather than forking.
ToolsOverride = Literal["auto", "on", "off"]
# What the model is *for* (#944). ``auto`` asks the catalogue; ``chat``/``embedding`` are the
# operator overruling it. Named ``role``, not ``mode``, because the value we read from LiteLLM's
# map is itself called ``mode`` and the two must stay distinguishable in the code.
RoleOverride = Literal["auto", "chat", "embedding"]
# What the gateway *learned* from the provider, as opposed to what the operator set. Only ever
# ``off`` today: a provider that rejects a tool list has told us it cannot call tools, while a
# provider that accepts one has told us nothing it wasn't already assumed to say (#947).
LearnedTools = Literal["off"]


class SavedModelOverride(BaseModel):
    """The operator's correction to a saved hosted model's *declared* capabilities (#711).

    This is **metadata**, not tuning. ``ModelSettings`` answers "how much context should this
    model *use*" (``num_ctx``, a runtime knob); this answers "what is this model *capable* of"
    — which the core otherwise takes from LiteLLM's static cost map. That map is missing
    entries entirely (``xai/grok-latest``) and mislabels others, and the consequence is real:
    a vision-capable model resolves to ``supports_vision() is False`` and the image gate (#633)
    refuses image turns for a model that would have handled them.

    Three capabilities ride here (ADR-0140): ``vision`` (#711), ``tools`` (#947) and ``role``
    (#944). ``tools_learned`` is the odd one out — it is **not** operator input but the
    gateway's own record of a provider rejecting a tool list, kept in its own field precisely
    so "Auto, and we learned it can't" stays distinguishable from the operator's explicit
    ``off``. Any operator write of this record clears it (see :meth:`SavedHostedModelStore.
    set_override`), so returning a control to Auto genuinely starts over.

    Defaults are the pre-override behaviour exactly — ``auto`` everything and no context length
    mean "ask the catalogue", so an absent or empty override changes nothing.
    """

    vision: VisionOverride = "auto"
    tools: ToolsOverride = "auto"
    role: RoleOverride = "auto"
    # The model's *declared* context length, for badges and as the ceiling on the
    # context-window suggestion. None = take the map's answer. Not the operator's chosen
    # num_ctx — that is ``ModelSettings.context_window``, a different layer.
    context_length: int | None = Field(default=None, gt=0)
    # Read-only from the operator's side: written by the gateway, cleared by any operator write.
    tools_learned: LearnedTools | None = None

    def is_empty(self) -> bool:
        """True when the record says nothing the catalogue doesn't already say."""
        return (
            self.vision == "auto"
            and self.tools == "auto"
            and self.role == "auto"
            and self.context_length is None
            and self.tools_learned is None
        )


def _now_ms() -> int:
    """Epoch milliseconds — the save timestamp. A module-level seam tests monkeypatch to
    make ordering deterministic without a real clock."""
    return int(time.time() * 1000)


def _to_override(
    vision: str | None,
    context_length: int | None,
    tools: str | None = None,
    role: str | None = None,
    tools_learned: str | None = None,
) -> SavedModelOverride:
    """Build the capability record from its stored columns, tolerating a stale/unknown value.

    A column outside its vocabulary (hand-edited SQL, or a value written by a newer build)
    degrades to the default rather than raising: a capability *hint* must never be able to
    break the model list it decorates.
    """
    vision_value: VisionOverride = "on" if vision == "on" else "off" if vision == "off" else "auto"
    tools_value: ToolsOverride = "on" if tools == "on" else "off" if tools == "off" else "auto"
    role_value: RoleOverride = (
        "chat" if role == "chat" else "embedding" if role == "embedding" else "auto"
    )
    learned: LearnedTools | None = "off" if tools_learned == "off" else None
    return SavedModelOverride(
        vision=vision_value,
        tools=tools_value,
        role=role_value,
        context_length=context_length,
        tools_learned=learned,
    )


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
    # ``text("0")``, not the bare string ``"0"``: a plain string is a *literal* SQLAlchemy
    # quotes, so that spelling emitted ``DEFAULT '0'`` — a quoted zero — from ``create_all``
    # while the additive reconcile pasted it in raw and emitted ``DEFAULT 0`` (#834, rev 0002).
    added_at: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    # The capability override (#711), stored flat. NULL means "no override" — the absence *is*
    # the "auto" case, so an untouched row keeps the catalogue's answers verbatim. All of these
    # are nullable with no server default, so the revision that adds them needs no backfill
    # (ADR-0138's backfill rule) and the baseline's additive reconcile restores them as-is.
    vision_override: Mapped[str | None] = mapped_column(String(8), nullable=True)
    context_length_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Tool calling (#947) and the model's role (#944), same vocabulary shape as vision.
    tools_override: Mapped[str | None] = mapped_column(String(8), nullable=True)
    role_override: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Not operator input: what the gateway learned when a provider rejected a tool list (#947).
    # Its own column so "Auto (learned: no tool support)" and an explicit "Not supported" stay
    # distinguishable in the sheet, and so returning the control to Auto genuinely starts over.
    tools_learned: Mapped[str | None] = mapped_column(String(8), nullable=True)


class SavedHostedModelStore:
    """Read/write the tenant's saved hosted-model ids (#496)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Build this store's tables from the models — the **unit-test** schema path.

        The deployed service does not call this: its schema comes from the revisions in
        :mod:`epicurus_core_app.migrations`, applied at startup (#834, ADR-0138). See that
        module's docstring for why ``create_all`` survives here, and what keeps it honest.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(_SavedBase.metadata.create_all)

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
                    _SavedModelRow.tools_override,
                    _SavedModelRow.role_override,
                    _SavedModelRow.tools_learned,
                ).where(_SavedModelRow.tenant == tenant)
            )
            out: dict[str, SavedModelOverride] = {}
            for model, vision, context_length, tools, role, learned in rows:
                override = _to_override(vision, context_length, tools, role, learned)
                if not override.is_empty():
                    out[model] = override
            return out

    async def get_override(self, tenant: str, model: str) -> SavedModelOverride:
        """One saved model's capability record — all-defaults when unsaved or unset (#711)."""
        async with self._session() as session:
            row = (
                await session.execute(
                    select(
                        _SavedModelRow.vision_override,
                        _SavedModelRow.context_length_override,
                        _SavedModelRow.tools_override,
                        _SavedModelRow.role_override,
                        _SavedModelRow.tools_learned,
                    ).where(_SavedModelRow.tenant == tenant, _SavedModelRow.model == model)
                )
            ).first()
        if row is None:
            return SavedModelOverride()
        return _to_override(row[0], row[1], row[2], row[3], row[4])

    async def set_override(self, tenant: str, model: str, override: SavedModelOverride) -> bool:
        """Store ``model``'s capability override; False when the model isn't saved (#711).

        An override is a property *of a saved row*, so this updates rather than upserts — the
        caller 404s an id the tenant hasn't saved instead of silently creating a row that the
        saved-model list would then surface. An empty override clears every column back to
        NULL, which is exactly the pre-override state.

        **The operator's write always clears ``tools_learned``** (ADR-0140). The learned answer
        is the gateway's guess about a deployment; the operator saying anything about this model
        — including returning the control to Auto — supersedes it, and the gateway learns again
        for real if the provider rejects a tool list again. Anything else would leave a stale
        "we learned it can't" quietly overriding a fresh Auto.
        """
        async with self._session() as session:
            result = await session.execute(
                update(_SavedModelRow)
                .where(_SavedModelRow.tenant == tenant, _SavedModelRow.model == model)
                .values(
                    vision_override=None if override.vision == "auto" else override.vision,
                    context_length_override=override.context_length,
                    tools_override=None if override.tools == "auto" else override.tools,
                    role_override=None if override.role == "auto" else override.role,
                    tools_learned=None,
                )
            )
            await session.commit()
        # A DML execute yields a CursorResult at runtime; the async signature is the wider
        # Result, which doesn't declare ``rowcount``. One UPDATE (not SELECT-then-UPDATE) so
        # "does this row exist" and "write it" can't disagree under concurrency.
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def learn_tools_unsupported(self, tenant: str, model: str) -> bool:
        """Record that ``model``'s provider rejected a tool list, for ``tenant`` (#947).

        Written to its own column, never to ``tools_override``: the operator's explicit choice
        and the gateway's learned one are different facts and the sheet has to be able to tell
        them apart. Persisted rather than cached in memory so the answer survives a restart and
        is the same on every replica (constraint #2), and scoped to the **calling** tenant —
        a provider's deployment is a per-tenant fact, since the key is.

        Returns False when the tenant has not saved this id (a local model, or a hosted one
        picked in a chat but not persisted): there is no row to remember it on, and the runtime
        answers for a local model anyway.
        """
        async with self._session() as session:
            result = await session.execute(
                update(_SavedModelRow)
                .where(_SavedModelRow.tenant == tenant, _SavedModelRow.model == model)
                .values(tools_learned="off")
            )
            await session.commit()
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
