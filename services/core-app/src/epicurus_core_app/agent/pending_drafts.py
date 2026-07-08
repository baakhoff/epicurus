"""Pending outbound drafts — the durable state behind draft-first send Confirm/Decline (ADR-0085).

When a module's *compose* tool returns a :class:`~epicurus_core.DraftReview` (mail's
``mail_send`` / ``mail_reply``, #563), the turn cannot finish until the operator approves the
draft, so the in-progress run is persisted here (the conversation so far, the pending tool-call
id, and the composed draft), the SSE stream ends with an ``awaiting_input`` frame, and a
Confirm/Decline request rehydrates and continues it.

This is a deliberate **sibling** to :mod:`epicurus_core_app.agent.suspended` (the ``ask_user``
store) rather than an extension of it: the shipped ``agent_suspended_runs`` table already exists,
and ``create_all`` never adds columns to an existing table — so a new, separately-created table
carries the draft-specific fields with no migration, and the two consume-on-resume paths can never
cross (a stray ``/resume`` cannot swallow a draft, or vice-versa). Rows are **consumed** on resume
and reaped after a TTL. Tenant-scoped (constraint #1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _Base(DeclarativeBase):
    pass


class _PendingDraft(_Base):
    """ORM row for one paused turn awaiting an operator's Confirm/Decline of a draft."""

    __tablename__ = "agent_pending_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The compose tool-call this draft answers, and its tool name — so the resumed turn appends
    # the send/decline outcome as *that* call's tool result (a valid, closed tool call).
    pending_call_id: Mapped[str] = mapped_column(String(255))
    tool: Mapped[str] = mapped_column(String(128))
    # The module whose transmit endpoint (``POST /send``) sends this draft on Confirm.
    module: Mapped[str] = mapped_column(String(128))
    # A one-line human/log label for the draft (e.g. "Email to bob@… — Re: Lunch").
    summary: Mapped[str] = mapped_column(Text, default="")
    # The composed draft: both what the shell shows and what the transmit endpoint sends, so what
    # the operator approves is byte-for-byte what goes out (ADR-0085). Opaque, channel-specific.
    draft: Mapped[dict[str, Any]] = mapped_column(JSON)
    # The conversation up to (and including) the assistant message that called the compose tool,
    # plus any sibling tool results — everything the loop needs to continue once the send/decline
    # outcome is appended as the pending call's tool result.
    conversation: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@dataclass(frozen=True)
class PendingDraft:
    """A rehydrated pending draft — enough to transmit and resume the turn."""

    session_id: str | None
    model: str | None
    pending_call_id: str
    tool: str
    module: str
    summary: str
    draft: dict[str, Any]
    conversation: list[dict[str, Any]]


class PendingDraftStore:
    """Persist and rehydrate pending outbound drafts (tenant-scoped)."""

    def __init__(self, engine: AsyncEngine, *, ttl_hours: int = 24) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)
        self._ttl = timedelta(hours=max(1, ttl_hours))

    async def init(self) -> None:
        """Create the schema."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def save(
        self,
        *,
        tenant: str,
        session_id: str | None,
        model: str | None,
        pending_call_id: str,
        tool: str,
        module: str,
        summary: str,
        draft: dict[str, Any],
        conversation: list[dict[str, Any]],
    ) -> str:
        """Persist a pending draft and return its generated ``run_id``.

        Opportunistically reaps rows older than the TTL first, so an abandoned draft is cleaned up
        without a separate scheduler (mirrors the ``ask_user`` store).
        """
        run_id = uuid.uuid4().hex
        cutoff = datetime.now(UTC) - self._ttl
        async with self._session() as session:
            await session.execute(delete(_PendingDraft).where(_PendingDraft.created_at < cutoff))
            session.add(
                _PendingDraft(
                    id=run_id,
                    tenant=tenant,
                    session_id=session_id,
                    model=model,
                    pending_call_id=pending_call_id,
                    tool=tool,
                    module=module,
                    summary=summary,
                    draft=draft,
                    conversation=conversation,
                )
            )
            await session.commit()
        return run_id

    async def take(self, *, tenant: str, run_id: str) -> PendingDraft | None:
        """Return and **delete** the pending draft, or ``None`` if absent/foreign-tenant.

        Consuming on read makes Confirm/Decline idempotent-safe: a double-submit finds nothing the
        second time rather than sending twice or replaying the turn.
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_PendingDraft).where(
                    _PendingDraft.tenant == tenant, _PendingDraft.id == run_id
                )
            )
            if row is None:
                return None
            data = PendingDraft(
                session_id=row.session_id,
                model=row.model,
                pending_call_id=row.pending_call_id,
                tool=row.tool,
                module=row.module,
                summary=row.summary,
                draft=dict(row.draft),
                conversation=list(row.conversation),
            )
            await session.delete(row)
            await session.commit()
            return data
