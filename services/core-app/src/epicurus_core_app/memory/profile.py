"""The standing user profile — a compact, durable picture of the user, synthesized off-hours.

Recall (ADR-0045/0051) surfaces the *long tail* — specific facts relevant to this turn — but pays
an embedding round-trip (and, on a single GPU, a model-swap risk) on the response path every turn,
even for the stable common case: who the user is, their durable preferences, standing context that
barely changes day to day. This module moves that common case off the turn: a nightly job distils
the fact store into one small **standing profile** (a few hundred tokens), stored per tenant and
versioned, and ``Agent._assemble`` injects it **statically** — no embed, no swap — while recall
stays for the specifics (ADR-0094). The same latency-shifting trade ADR-0051 made for fact
*extraction*, now for the profile the assistant carries.

Everything here is best-effort: no profile means exactly today's behavior, and a failed synthesis
keeps the previous profile rather than wiping it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol, cast

from pydantic import BaseModel
from sqlalchemy import CursorResult, DateTime, Integer, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from epicurus_core import get_logger
from epicurus_core_app.llm.models import ChatMessage, ChatResult
from epicurus_core_app.llm.power import GatewayPausedError
from epicurus_core_app.memory.facts import UserFact
from epicurus_core_app.memory.store import Base

log = get_logger("epicurus_core_app.memory.profile")

#: How a stored profile was produced — nightly synthesis, or an operator edit in the memory view.
SOURCE_AUTO = "auto"
SOURCE_EDITED = "edited"

# Bounds so one synthesis can't blow up the prompt or store an essay: how many facts we feed the
# synthesizer, and how long a profile we keep. A standing profile is meant to be compact — a few
# hundred tokens — so the static injection stays cheap and the model reads it every turn.
_MAX_FACTS = 200
_PROFILE_CAP = 4000

_SYSTEM_PROMPT = (
    "You maintain a compact STANDING PROFILE of the user for a personal assistant. From the "
    "durable facts below, write a short profile the assistant can read at the start of every "
    "conversation — who the user is, their stable preferences, how they like the assistant to "
    "behave, and their ongoing context.\n\n"
    "Rules:\n"
    "- Keep it SHORT — a few hundred tokens at most. It is read every turn; brevity is the point.\n"
    "- Group related facts into a few natural sentences or terse bullet lines; do not just list "
    "every fact verbatim.\n"
    "- Only the durable, general picture — omit one-off details and anything that reads as a "
    "passing task.\n"
    "- Write in the third person ('The user …'). No preamble, no headings like 'Profile:', no "
    "meta commentary — just the profile text itself.\n"
    "- If the facts don't support a meaningful profile, respond with an empty line."
)


class StandingProfile(BaseModel):
    """One stored version of the user's standing profile (tenant-scoped)."""

    id: int
    content: str
    source: str = SOURCE_AUTO
    created_at: datetime | None = None


class StoredProfile(Base):
    """One version of a tenant's standing profile — append-only, capped, newest injected."""

    __tablename__ = "standing_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_AUTO)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StandingProfileStore:
    """Stores versioned standing profiles per tenant (Postgres); the newest is what's injected.

    Versioning mirrors the editor's snapshot-on-save idiom (ADR-0046): each write appends a row and
    prunes to ``max_versions`` newest per tenant, so an operator can see how the profile evolved and
    a bad synthesis is recoverable, without the table growing unbounded.
    """

    def __init__(self, engine: AsyncEngine, *, max_versions: int = 5) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)
        self._max_versions = max(1, max_versions)

    async def init(self) -> None:
        """Create the table if it doesn't exist (idempotent; shares the store's Base)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def latest(self, *, tenant: str) -> StandingProfile | None:
        """The tenant's current profile (newest version), or ``None`` if none exists."""
        async with self._session() as session:
            row = (
                await session.scalars(
                    select(StoredProfile)
                    .where(StoredProfile.tenant == tenant)
                    .order_by(StoredProfile.id.desc())
                    .limit(1)
                )
            ).first()
            return self._to_model(row) if row is not None else None

    async def versions(self, *, tenant: str, limit: int = 5) -> list[StandingProfile]:
        """The tenant's recent profile versions, newest first (for the memory view to browse)."""
        async with self._session() as session:
            rows = await session.scalars(
                select(StoredProfile)
                .where(StoredProfile.tenant == tenant)
                .order_by(StoredProfile.id.desc())
                .limit(limit)
            )
            return [self._to_model(row) for row in rows]

    async def save(
        self, *, tenant: str, content: str, source: str = SOURCE_AUTO
    ) -> StandingProfile:
        """Append a new profile version, prune to ``max_versions``, and return it.

        Trims to :data:`_PROFILE_CAP` so one run can't store an essay. Pruning happens in the same
        session as the insert, keeping the tenant's history bounded on every write.
        """
        content = content.strip()[:_PROFILE_CAP]
        async with self._session() as session:
            row = StoredProfile(tenant=tenant, content=content, source=source)
            session.add(row)
            await session.flush()  # assign row.id before we prune around it
            keep = list(
                await session.scalars(
                    select(StoredProfile.id)
                    .where(StoredProfile.tenant == tenant)
                    .order_by(StoredProfile.id.desc())
                    .limit(self._max_versions)
                )
            )
            await session.execute(
                delete(StoredProfile).where(
                    StoredProfile.tenant == tenant, StoredProfile.id.notin_(keep)
                )
            )
            await session.commit()
            return self._to_model(row)

    async def clear(self, *, tenant: str) -> int:
        """Delete every stored profile version for a tenant; returns how many were removed.

        Resets to no-profile (exactly today's behavior) and lets the next synthesis regenerate a
        fresh ``auto`` profile — the operator's escape hatch out of a pinned edit.
        """
        async with self._session() as session:
            result = await session.execute(
                delete(StoredProfile).where(StoredProfile.tenant == tenant)
            )
            await session.commit()
            return cast("CursorResult[object]", result).rowcount or 0

    @staticmethod
    def _to_model(row: StoredProfile) -> StandingProfile:
        return StandingProfile(
            id=row.id, content=row.content, source=row.source, created_at=row.created_at
        )


class _ChatModel(Protocol):
    """The slice of the LLM gateway the synthesizer needs (eases faking in tests)."""

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = ...,
        tools: list[dict[str, object]] | None = ...,
        tenant_id: str | None = ...,
    ) -> ChatResult: ...


class _FactSource(Protocol):
    """The slice of the fact store the synthesizer reads (the corpus it distils)."""

    async def list_facts(self, *, tenant: str, limit: int = ...) -> list[UserFact]: ...


class ProfileSynthesizer:
    """Distils a tenant's fact store into one compact standing profile (best-effort).

    Runs on the nightly maintenance batch (ADR-0060 registry), never on the response path. Reads
    the durable facts, asks the gateway (constraint #8 — synthesis is metered like any inference)
    for a compact profile, and stores it. ``tenants`` yields the tenants to synthesize for, so the
    fan-out is tenant-first (constraint #1) even though v1 has one.
    """

    def __init__(
        self,
        chat: _ChatModel,
        facts: _FactSource,
        store: StandingProfileStore,
        *,
        tenants: Callable[[], Awaitable[list[str]]],
        model: str | None = None,
        max_facts: int = _MAX_FACTS,
    ) -> None:
        self._chat = chat
        self._facts = facts
        self._store = store
        self._tenants = tenants
        self._model = model
        self._max_facts = max_facts

    async def synthesize(self, *, tenant: str) -> StandingProfile | None:
        """Synthesize and store one tenant's profile, or ``None`` when nothing was written.

        Returns ``None`` (leaving any existing profile intact) when the operator has **pinned** an
        edited profile (see :meth:`_is_pinned` — corrections survive re-synthesis, the #527
        constraint), when the tenant has no facts yet (no profile → today's behavior), or when the
        model returns nothing usable (a failed synthesis keeps the previous profile). A paused
        gateway raises :class:`GatewayPausedError` for :meth:`run` to stop the batch on.
        """
        if await self._is_pinned(tenant):
            return None
        facts = await self._facts.list_facts(tenant=tenant, limit=self._max_facts)
        if not facts:
            return None
        prompt = "Durable facts about the user:\n" + "\n".join(f"- {fact.text}" for fact in facts)
        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content=prompt),
        ]
        result = await self._chat.chat(messages, model=self._model, tenant_id=tenant)
        content = (result.content or "").strip()
        if not content:
            return None  # the model declined / returned junk — keep whatever profile we had
        saved = await self._store.save(tenant=tenant, content=content, source=SOURCE_AUTO)
        log.info("synthesized standing profile", tenant=tenant, chars=len(saved.content))
        return saved

    async def _is_pinned(self, tenant: str) -> bool:
        """Whether the tenant's current profile is an operator edit that synthesis must preserve.

        The corrections-survive-re-synthesis policy lives here alone, so it is easy to change. v1
        pins the *whole* profile once edited: an ``edited`` current version is never overwritten by
        synthesis, so an operator correction is durable. The trade-off — the profile then stops
        auto-refreshing until the operator clears it (``StandingProfileStore.clear``) — is the safe
        default; finer line-level pinning or writing edits back as facts is a future refinement.
        """
        latest = await self._store.latest(tenant=tenant)
        return latest is not None and latest.source == SOURCE_EDITED

    async def run(self) -> int:
        """Synthesize every tenant's profile once; returns how many profiles were written.

        The nightly-job entry point. Best-effort per tenant — one tenant's failure is logged and
        skipped, never aborting the rest — but a paused gateway stops the batch (the model is
        asleep; leave the remainder for the next window), mirroring the extraction drain.
        """
        written = 0
        for tenant in await self._tenants():
            try:
                if await self.synthesize(tenant=tenant) is not None:
                    written += 1
            except GatewayPausedError:
                log.info("profile synthesis stopped; gateway paused", done=written)
                break
            except Exception as exc:  # one tenant's failure must not wedge the batch
                log.warning("profile synthesis failed for a tenant", tenant=tenant, error=str(exc))
        if written:
            log.info("nightly profile synthesis complete", profiles=written)
        return written
