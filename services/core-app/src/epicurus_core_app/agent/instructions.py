"""The agent's base system prompt — the editable instructions (tenant-scoped) (#497, ADR-0083).

Until now the agent ran with **no** base system prompt: identity and behaviour were emergent
from the tool schemas and the model's own defaults. This stores an operator-editable prompt,
one row per tenant, that the agent injects as the **first** system message of every turn
(``Agent._assemble``) — chat and headless bridge turns alike — ahead of recalled memory and
attached context, where the compaction prefix rule protects it from being trimmed.

A NULL/absent row falls back to the shipped :data:`DEFAULT_AGENT_INSTRUCTIONS`. Follows the
``TimezonePrefsStore`` pattern (ADR-0039): auto-created and column-healed on ``init()``, resolved
per turn so an edit takes effect on the next turn with no restart. The memory-extraction prompt
(``memory/extraction.py``) is a separate pipeline and out of scope.

Since ADR-0093 this store composes rather than merely reads: :meth:`AgentInstructionsStore.
get_instructions` returns the base prompt **plus every enabled named playbook**
(``playbooks.py``), each under its own heading, as one opaque string. ``Agent._assemble``'s call
site is unchanged — the composition happens *below* the accessor, which is precisely why the ADR
needed no ``_assemble`` change. It also gained ADR-0046 snapshot-on-save versioning, which it
never needed while the operator was the only author: an *agent-proposed* edit the operator later
regrets needs an undo.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, String, Text, delete, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import get_logger
from epicurus_core.db import ensure_columns
from epicurus_core_app.agent.playbooks import MAX_VERSIONS, PlaybookStore

log = get_logger("epicurus_core_app.agent.instructions")

# The built-in default prompt shipped when a tenant hasn't set its own (#497). It establishes who
# epicurus is (a private, self-hosted, single-operator assistant), a concise and candid voice,
# tool-use guidance, and the source-grounding ladder (#703: module data first, then web search,
# never an unsourced guess) — deliberately with no date/time baked in (the `now` tool owns that,
# #267). The operator's edit replaces it per tenant; clearing the edit falls back here.
DEFAULT_AGENT_INSTRUCTIONS = """\
You are the assistant at the heart of epicurus — a private, self-hosted, local-first personal \
assistant that runs on the operator's own machine. You help one person, your operator, across \
whatever their installed modules expose: calendar, tasks, notes, mail, files, and their \
knowledge base.

Voice: warm, plain-spoken, and concise. Lead with the answer, then add only the context that \
earns its place — a direct sentence over a hedge, a short list over a wall of prose. Match the \
operator's register and skip filler pleasantries and boilerplate disclaimers.

Doing things: act through the tools you are given — use them to read real state and to make \
changes rather than guessing or narrating what you would do. Prefer one good tool call over \
asking a question you could answer yourself, but when a request is genuinely ambiguous, or a \
change is destructive or hard to undo, confirm first. If a tool fails, say what happened plainly \
instead of pretending it worked, and never invent an event, message, file, or fact you have not \
actually seen.

Finding answers: ground what you say in something you have actually read. For anything about \
the operator's own world, check their modules first — the knowledge base, notes, calendar, \
tasks, mail, files. When those come up empty, or the question concerns the wider world — news, \
releases, prices, schedules, anything that may have changed lately — search the web rather than \
relying on what you remember from training, which is stale. Answer from a source, or say \
plainly that you looked and found nothing; never dress a guess up as a fact.

Boundaries: everything here belongs to the operator and stays on their machine. Be candid about \
what you can and cannot do, don't claim capabilities you lack, and if you don't know, say so."""


@dataclass(frozen=True)
class InstructionsVersion:
    """One snapshot of the base prompt's prior content (ADR-0046).

    ``content`` is ``None`` for list rows and populated for a single fetched version; ``size`` is
    the snapshot's character count. Mirrors ``playbooks.PlaybookVersion``.
    """

    version_id: str
    created_at: datetime
    size: int
    content: str | None = None


class _InstrBase(DeclarativeBase):
    pass


class _AgentInstructionsRow(_InstrBase):
    """One operator-editable system prompt per tenant."""

    __tablename__ = "agent_instructions"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    # The operator's prompt; NULL (or blank) means fall back to DEFAULT_AGENT_INSTRUCTIONS.
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)


class _AgentInstructionsVersionRow(_InstrBase):
    """One snapshot of a tenant's base prompt (ADR-0046's shape, per tenant).

    A parallel table to ``agent_playbook_versions`` rather than one shared version stream: the
    base prompt is a per-tenant singleton and a playbook is one of N named documents, so mixing
    them would turn "roll back *this* playbook" into "figure out which interleaved version
    belongs to which document" (ADR-0093's own rejected alternative).
    """

    __tablename__ = "agent_instructions_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    vid: Mapped[str] = mapped_column(String(32), index=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentInstructionsStore:
    """Read/write the operator's base system prompt for a tenant (#497).

    Given a *playbooks* store, :meth:`get_instructions` composes the base prompt with every
    enabled playbook (ADR-0093 §4); without one it behaves exactly as it did before playbooks
    existed, which is what the store's own unit tests and any minimal wiring rely on.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        default: str = DEFAULT_AGENT_INSTRUCTIONS,
        playbooks: PlaybookStore | None = None,
    ) -> None:
        self._engine = engine
        self._default = default
        self._playbooks = playbooks
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    @property
    def default(self) -> str:
        """The shipped default prompt used when the tenant has set none."""
        return self._default

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_InstrBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Reconcile columns added after first release via the shared additive helper (#249)."""
        ensure_columns(sync_conn, _AgentInstructionsRow.__table__, ("instructions",))

    async def get_instructions(self, tenant: str) -> str:
        """The **composed** prompt: the base instructions plus every enabled playbook (ADR-0093 §4).

        Called by ``Agent._assemble`` per turn, so an edit takes effect on the next turn. The
        composition happens here rather than at the call site, so ``_assemble`` still leads the
        turn with one opaque string and never learned that playbooks exist.

        A playbook read that fails degrades to the base prompt alone rather than breaking the
        turn — the same best-effort rule the standing profile follows (ADR-0094). The base
        prompt is the thing the agent cannot run sensibly without; an enabled playbook is
        enrichment.
        """
        base = await self.get_base(tenant)
        if self._playbooks is None:
            return base
        try:
            extra = await self._playbooks.compose(tenant)
        except Exception as exc:  # enrichment must never cost the operator a turn
            log.warning("playbook composition failed; using base instructions", error=str(exc))
            return base
        return f"{base}\n\n{extra}" if extra else base

    async def get_base(self, tenant: str) -> str:
        """The effective **base** prompt alone: the stored value if set, else the shipped default.

        The pre-playbooks meaning of :meth:`get_instructions`, kept as its own accessor because
        two callers genuinely want the base without the playbooks composed in: this method's own
        composition step, and the review surface, which diffs a proposed base-instructions edit
        against what is actually stored (never against base+playbooks, which is not a document
        anyone can edit).
        """
        async with self._session() as session:
            row = await session.get(_AgentInstructionsRow, tenant)
            if row is None or not row.instructions or not row.instructions.strip():
                return self._default
            return row.instructions

    async def get_raw(self, tenant: str) -> str | None:
        """The stored prompt, or ``None`` when unset (so the route can flag ``is_default``)."""
        async with self._session() as session:
            row = await session.get(_AgentInstructionsRow, tenant)
            if row is None or not row.instructions or not row.instructions.strip():
                return None
            return row.instructions

    async def set_instructions(self, tenant: str, value: str | None) -> None:
        """Set the operator's prompt, or clear it (blank/``None`` → back to the default).

        Snapshots the **previous** effective base prompt first (ADR-0046), so an approved
        agent-proposed edit — or an operator's own overwrite — is always undoable. Snapshotting
        the *effective* value (not the raw row) means the very first edit records the shipped
        default, so "put it back how it was" works even for a tenant that had never customized
        it. See ``PlaybookStore.save`` for why this snapshots the replaced body rather than the
        saved one, the single deliberate departure from the editor's version store.

        A write that leaves the effective prompt unchanged records no version — the editor's
        "a save that changed nothing must not pile up duplicates" rule.
        """
        cleaned = value.strip() if value else ""
        previous = await self.get_base(tenant)
        async with self._session() as session:
            row = await session.get(_AgentInstructionsRow, tenant)
            if not cleaned:
                if row is not None:
                    row.instructions = None
            else:
                if row is None:
                    row = _AgentInstructionsRow(tenant=tenant)
                    session.add(row)
                row.instructions = cleaned
            await session.commit()
        if (cleaned or self._default) != previous:
            await self._snapshot(tenant, previous)

    async def _snapshot(self, tenant: str, content: str) -> None:
        """Record one snapshot of the base prompt, deduplicated, pruned to :data:`MAX_VERSIONS`.

        The ADR-0046 shape verbatim, sharing ``playbooks.MAX_VERSIONS`` so the two halves of
        ADR-0093 §3's "capped at the same MAX_VERSIONS" cannot drift apart.
        """
        async with self._session() as session:
            newest = await session.scalar(
                select(_AgentInstructionsVersionRow.content)
                .where(_AgentInstructionsVersionRow.tenant == tenant)
                .order_by(_AgentInstructionsVersionRow.id.desc())
                .limit(1)
            )
            if newest == content:
                return
            session.add(
                _AgentInstructionsVersionRow(vid=uuid.uuid4().hex, tenant=tenant, content=content)
            )
            await session.commit()
            keep_ids = (
                await session.scalars(
                    select(_AgentInstructionsVersionRow.id)
                    .where(_AgentInstructionsVersionRow.tenant == tenant)
                    .order_by(_AgentInstructionsVersionRow.id.desc())
                    .limit(MAX_VERSIONS)
                )
            ).all()
            await session.execute(
                delete(_AgentInstructionsVersionRow).where(
                    _AgentInstructionsVersionRow.tenant == tenant,
                    _AgentInstructionsVersionRow.id.notin_(keep_ids),
                )
            )
            await session.commit()

    async def versions(self, tenant: str) -> list[InstructionsVersion]:
        """The base prompt's snapshots, newest first — bodies omitted (ADR-0046's list shape)."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_AgentInstructionsVersionRow)
                .where(_AgentInstructionsVersionRow.tenant == tenant)
                .order_by(_AgentInstructionsVersionRow.id.desc())
            )
            return [
                InstructionsVersion(
                    version_id=row.vid, created_at=row.created_at, size=len(row.content)
                )
                for row in rows
            ]

    async def version(self, tenant: str, version_id: str) -> InstructionsVersion | None:
        """One snapshot **with** its body, or ``None`` — what a rollback reads."""
        async with self._session() as session:
            row = await session.scalar(
                select(_AgentInstructionsVersionRow).where(
                    _AgentInstructionsVersionRow.tenant == tenant,
                    _AgentInstructionsVersionRow.vid == version_id,
                )
            )
            if row is None:
                return None
            return InstructionsVersion(
                version_id=row.vid,
                created_at=row.created_at,
                size=len(row.content),
                content=row.content,
            )
