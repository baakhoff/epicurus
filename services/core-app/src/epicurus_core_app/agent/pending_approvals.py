"""Pending operator approvals — the durable state behind ``ask_approval`` (ADR-0117).

When the model calls ``ask_approval`` after staging a change through an existing propose tool
(``knowledge_propose_edit``, and any future module's equivalent), the turn cannot finish until
the operator Approves or Rejects, so the in-progress run is persisted here (the conversation so
far, the pending tool-call id, and the summary/refs the model gave the operator to decide from),
the SSE stream ends with an ``awaiting_input`` frame, and an Approve/Reject request rehydrates
and continues it.

This is a deliberate **sibling** to :mod:`epicurus_core_app.agent.suspended` (the ``ask_user``
store) and :mod:`epicurus_core_app.agent.pending_drafts` (the ``draft_review`` store), for the
same reason ``pending_drafts`` gives for not extending ``suspended``: both shipped tables already
exist, and ``create_all`` never adds columns to an existing table, so a third pause kind gets a
third, separately-created table rather than a migration. The three consume-on-resume paths can
never cross — a stray ``/resume`` or ``/draft`` post cannot swallow an approval, or vice versa.
Rows are **consumed** on resume and reaped after a TTL. Tenant-scoped (constraint #1).

Unlike a draft, an approval never carries a payload the core transmits on the operator's behalf:
the actual Approve/Reject action is the *module's own* existing review-decision endpoint, called
directly by the web client (so the agent can never approve its own work — see
``suggestions.py``'s boundary comment). This store only remembers enough to resume the
*conversation* once that decision is known — there is no ``module``/``tool`` column to record,
since ``ask_approval`` is always the one fixed tool name (unlike a draft's compose tool, which
varies per module).
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


class _PendingApproval(_Base):
    """ORM row for one paused turn awaiting an operator's Approve/Reject."""

    __tablename__ = "agent_pending_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The ask_approval tool-call this decision answers — always ASK_APPROVAL_TOOL, so (unlike
    # pending_drafts) no separate `tool` column is needed to reconstruct the resumed result.
    pending_call_id: Mapped[str] = mapped_column(String(255))
    # A one-line human/log label for what's being approved (the model's own summary).
    summary: Mapped[str] = mapped_column(Text, default="")
    # The entity reference(s) the model gave for the staged change (module/kind/ref_id/title),
    # so the operator's approval card can link straight to it. Possibly empty — a propose tool
    # that doesn't yet return a ref leaves the operator with the summary text alone.
    refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    # The conversation up to (and including) the assistant message that called ask_approval,
    # plus any sibling tool results — everything the loop needs to continue once the operator's
    # decision is appended as the pending call's tool result.
    conversation: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@dataclass(frozen=True)
class PendingApproval:
    """A rehydrated pending approval — enough to resume the turn once a decision is known."""

    session_id: str | None
    model: str | None
    pending_call_id: str
    summary: str
    refs: list[dict[str, Any]]
    conversation: list[dict[str, Any]]


class PendingApprovalStore:
    """Persist and rehydrate pending ``ask_approval`` pauses (tenant-scoped)."""

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
        summary: str,
        refs: list[dict[str, Any]],
        conversation: list[dict[str, Any]],
    ) -> str:
        """Persist a pending approval and return its generated ``run_id``.

        Opportunistically reaps rows older than the TTL first, so an abandoned approval is
        cleaned up without a separate scheduler (mirrors the ``ask_user`` / draft stores).
        """
        run_id = uuid.uuid4().hex
        cutoff = datetime.now(UTC) - self._ttl
        async with self._session() as session:
            await session.execute(
                delete(_PendingApproval).where(_PendingApproval.created_at < cutoff)
            )
            session.add(
                _PendingApproval(
                    id=run_id,
                    tenant=tenant,
                    session_id=session_id,
                    model=model,
                    pending_call_id=pending_call_id,
                    summary=summary,
                    refs=refs,
                    conversation=conversation,
                )
            )
            await session.commit()
        return run_id

    async def take(self, *, tenant: str, run_id: str) -> PendingApproval | None:
        """Return and **delete** the pending approval, or ``None`` if absent/foreign-tenant.

        Consuming on read makes Approve/Reject idempotent-safe: a double-submit finds nothing
        the second time rather than resuming the same turn twice.
        """
        async with self._session() as session:
            row = await session.scalar(
                select(_PendingApproval).where(
                    _PendingApproval.tenant == tenant, _PendingApproval.id == run_id
                )
            )
            if row is None:
                return None
            data = PendingApproval(
                session_id=row.session_id,
                model=row.model,
                pending_call_id=row.pending_call_id,
                summary=row.summary,
                refs=list(row.refs),
                conversation=list(row.conversation),
            )
            await session.delete(row)
            await session.commit()
            return data
