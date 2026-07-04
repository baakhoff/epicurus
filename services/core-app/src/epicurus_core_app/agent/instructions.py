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
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.db import ensure_columns

# The built-in default prompt shipped when a tenant hasn't set its own (#497). It establishes who
# epicurus is (a private, self-hosted, single-operator assistant), a concise and candid voice, and
# tool-use guidance — deliberately with no date/time baked in (the `now` tool owns that, #267). The
# operator's edit replaces it per tenant; clearing the edit falls back here.
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

Boundaries: everything here belongs to the operator and stays on their machine. Be candid about \
what you can and cannot do, don't claim capabilities you lack, and if you don't know, say so."""


class _InstrBase(DeclarativeBase):
    pass


class _AgentInstructionsRow(_InstrBase):
    """One operator-editable system prompt per tenant."""

    __tablename__ = "agent_instructions"

    tenant: Mapped[str] = mapped_column(String(63), primary_key=True)
    # The operator's prompt; NULL (or blank) means fall back to DEFAULT_AGENT_INSTRUCTIONS.
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentInstructionsStore:
    """Read/write the operator's base system prompt for a tenant (#497)."""

    def __init__(self, engine: AsyncEngine, *, default: str = DEFAULT_AGENT_INSTRUCTIONS) -> None:
        self._engine = engine
        self._default = default
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
        """The effective prompt: the stored value if set (non-blank), else the shipped default.

        Called by ``Agent._assemble`` per turn, so an edit takes effect on the next turn.
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
        """Set the operator's prompt, or clear it (blank/``None`` → back to the default)."""
        cleaned = value.strip() if value else ""
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
