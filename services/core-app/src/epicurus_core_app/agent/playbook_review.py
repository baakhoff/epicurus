"""The core's own ``review`` page — governed approval for instruction/playbook edits (ADR-0093 §2).

The agent never rewrites its own guidance. The nightly reflection pass (ADR-0093 §1) *stages* a
proposal here; the operator approves or rejects it; only an approval writes through
``instructions.py`` / ``playbooks.py``. Every path terminates at that Approve — the ADR's hard
non-goal is that nothing self-applies, ever.

**Why this file exists at all.** Every other ``review``-page implementer is an external module the
core reaches over HTTP through ``ModuleRegistry``'s probe; core-app hosts no page of its own. The
ADR rejected bending core-app into "a module that calls itself over HTTP" as needless indirection,
and chose instead a **reserved pseudo-module** named :data:`CORE_MODULE_NAME` that the registry
answers **in-process**. Everything else is reuse: a proposal is a plain
:class:`~epicurus_core.review.ReviewSuggestion` (#542/ADR-0090), so the existing, unmodified
``ReviewView`` / ``SuggestionReviewModal`` render it exactly like knowledge's or notes' queue —
the diff, the editable draft, and the audit trail all come for free.

**Storage** mirrors the module-side precedent (ADR-0090): the pending queue *is* the set of rows
(a resolved row leaves it), and a durable :class:`~epicurus_core.review.ReviewDecision` trail
records what was proposed versus what was actually applied — which is also what the reflection
job reads back as negative context so a declined idea isn't re-proposed unchanged (ADR-0093 §6).
"""

from __future__ import annotations

import difflib
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import DateTime, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core.manifest import ModuleManifest, PageSpec, UiSection
from epicurus_core.review import (
    ApplyResult,
    ReviewAuditData,
    ReviewData,
    ReviewDecision,
    ReviewSuggestion,
)
from epicurus_core_app.agent.instructions import AgentInstructionsStore
from epicurus_core_app.agent.playbooks import PlaybookStore

# The reserved pseudo-module name (ADR-0093 §2). Not a configured base URL and never probed over
# HTTP — ``ModuleRegistry`` recognises it and dispatches in-process. Reserved: a real module may
# not claim it (the registry rejects the management writes for this name).
CORE_MODULE_NAME = "core"

# The core's single review page id. ``/m/core/playbooks`` is its route, though the shell folds
# every ``review`` page into the unified Suggestions inbox rather than giving it a nav entry.
CORE_REVIEW_PAGE_ID = "playbooks"

# The reserved ``path`` identifying a proposal against the base system prompt (ADR-0083), as
# opposed to ``PLAYBOOK_PATH_PREFIX``-prefixed paths naming an individual playbook. ``path`` is
# the review contract's document identity; these two shapes are the only ones the core proposes.
INSTRUCTIONS_PATH = "instructions"
PLAYBOOK_PATH_PREFIX = "playbooks/"

# Retention for the resolved-decision trail, mirroring the module-side cap (ADR-0090).
MAX_DECISIONS = 200

# The operations the core's review queue accepts: "update" an existing document (the base
# instructions, or an existing playbook) and "create" a new named playbook (ADR-0093 §2). No
# delete/move — removing a playbook is an operator action, never something the agent proposes.
_OPERATIONS = frozenset({"create", "update"})


def playbook_path(name: str) -> str:
    """The review-contract ``path`` identifying the playbook called *name*."""
    return f"{PLAYBOOK_PATH_PREFIX}{name}"


def playbook_name_from_path(path: str) -> str | None:
    """The playbook name in *path*, or ``None`` if it doesn't name a playbook."""
    if not path.startswith(PLAYBOOK_PATH_PREFIX):
        return None
    return path[len(PLAYBOOK_PATH_PREFIX) :] or None


def _unified_diff(path: str, before: str, after: str) -> str:
    """A unified diff from *before* to *after*, labelled with *path* (the ADR-0090 shape)."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


def _title_for(path: str) -> str:
    """The operator-facing title of the document *path* names."""
    name = playbook_name_from_path(path)
    return name if name is not None else "Base instructions"


@dataclass(frozen=True)
class PlaybookProposal:
    """One staged, not-yet-resolved proposal — an immutable projection of a stored row."""

    sid: str
    path: str
    operation: str
    proposed_content: str
    origin: str
    note: str
    created_at: datetime


class _ProposalBase(DeclarativeBase):
    pass


class _StoredProposal(_ProposalBase):
    """One pending proposal against the base instructions or a playbook, scoped to a tenant."""

    __tablename__ = "agent_playbook_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    sid: Mapped[str] = mapped_column(String(32), index=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    path: Mapped[str] = mapped_column(String(512))
    operation: Mapped[str] = mapped_column(String(16))
    proposed_content: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(64), default="reflection")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class _StoredDecision(_ProposalBase):
    """One resolved proposal: what was proposed alongside what was actually applied (ADR-0090)."""

    __tablename__ = "agent_playbook_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    sid: Mapped[str] = mapped_column(String(32))
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    path: Mapped[str] = mapped_column(String(512))
    operation: Mapped[str] = mapped_column(String(16))
    origin: Mapped[str] = mapped_column(String(64), default="reflection")
    note: Mapped[str] = mapped_column(Text, default="")
    proposed_content: Mapped[str] = mapped_column(Text, default="")
    applied_content: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(16))
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlaybookProposalStore:
    """Tenant-scoped pending queue + resolved-decision trail for core proposals (ADR-0090)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create the schema if it does not exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_ProposalBase.metadata.create_all)

    async def add(
        self,
        *,
        tenant: str,
        path: str,
        operation: str,
        proposed_content: str,
        origin: str = "reflection",
        note: str = "",
    ) -> PlaybookProposal:
        """Stage a proposal and return it (with its freshly minted ``sid``)."""
        if operation not in _OPERATIONS:
            raise ValueError(f"unsupported operation: {operation!r}")
        sid = uuid.uuid4().hex
        async with self._session() as session:
            row = _StoredProposal(
                sid=sid,
                tenant=tenant,
                path=path,
                operation=operation,
                proposed_content=proposed_content,
                origin=origin,
                note=note,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _proposal_value(row)

    async def list_pending(self, *, tenant: str) -> list[PlaybookProposal]:
        """All pending proposals for *tenant*, oldest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredProposal)
                .where(_StoredProposal.tenant == tenant)
                .order_by(_StoredProposal.created_at, _StoredProposal.id)
            )
            return [_proposal_value(row) for row in rows]

    async def get(self, *, tenant: str, sid: str) -> PlaybookProposal | None:
        """One pending proposal by its opaque ``sid``, or ``None``."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredProposal).where(
                    _StoredProposal.tenant == tenant, _StoredProposal.sid == sid
                )
            )
            return _proposal_value(row) if row is not None else None

    async def delete(self, *, tenant: str, sid: str) -> bool:
        """Drop a resolved proposal from the queue. True if a row was deleted."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredProposal).where(
                    _StoredProposal.tenant == tenant, _StoredProposal.sid == sid
                )
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def record(
        self,
        *,
        tenant: str,
        proposal: PlaybookProposal,
        decision: str,
        applied_content: str = "",
    ) -> None:
        """Record one resolved proposal, then prune beyond :data:`MAX_DECISIONS` (ADR-0090).

        Written **before** the pending row drops, so a crash between the two leaves an audited
        decision and a stale queue row (visible, re-resolvable) rather than a silently vanished
        proposal with no trail.
        """
        async with self._session() as session:
            session.add(
                _StoredDecision(
                    sid=proposal.sid,
                    tenant=tenant,
                    path=proposal.path,
                    operation=proposal.operation,
                    origin=proposal.origin,
                    note=proposal.note,
                    proposed_content=proposal.proposed_content,
                    applied_content=applied_content,
                    decision=decision,
                    proposed_at=proposal.created_at,
                )
            )
            await session.commit()
            keep_ids = (
                await session.scalars(
                    select(_StoredDecision.id)
                    .where(_StoredDecision.tenant == tenant)
                    .order_by(_StoredDecision.id.desc())
                    .limit(MAX_DECISIONS)
                )
            ).all()
            await session.execute(
                delete(_StoredDecision).where(
                    _StoredDecision.tenant == tenant,
                    _StoredDecision.id.notin_(keep_ids),
                )
            )
            await session.commit()

    async def decisions(
        self, *, tenant: str, limit: int = 50, decision: str | None = None
    ) -> list[ReviewDecision]:
        """The resolved-decision trail, newest first (ADR-0090).

        *decision* filters to one outcome — the reflection job passes ``"rejected"`` to build its
        negative-context digest (ADR-0093 §6) without dragging approvals through the prompt.
        """
        async with self._session() as session:
            stmt = select(_StoredDecision).where(_StoredDecision.tenant == tenant)
            if decision is not None:
                stmt = stmt.where(_StoredDecision.decision == decision)
            rows = await session.scalars(stmt.order_by(_StoredDecision.id.desc()).limit(limit))
            return [
                ReviewDecision(
                    id=row.sid,
                    title=_title_for(row.path),
                    path=row.path,
                    operation=row.operation,
                    origin=row.origin,
                    note=row.note,
                    created_at=row.proposed_at.isoformat(),
                    decided_at=row.decided_at.isoformat(),
                    decision=row.decision,
                    proposed_content=row.proposed_content,
                    applied_content=row.applied_content,
                )
                for row in rows
            ]


def _proposal_value(row: _StoredProposal) -> PlaybookProposal:
    return PlaybookProposal(
        sid=row.sid,
        path=row.path,
        operation=row.operation,
        proposed_content=row.proposed_content,
        origin=row.origin,
        note=row.note,
        created_at=row.created_at,
    )


class CoreReviewPage:
    """The reserved ``core`` pseudo-module: a ``review`` page served in-process (ADR-0093 §2).

    ``ModuleRegistry`` dispatches to this instead of probing an HTTP base. The surface it exposes
    is deliberately the *same* one a real module serves over HTTP — :meth:`manifest`,
    :meth:`get_page`, :meth:`review_action`, :meth:`review_audit` — so the registry's branch is a
    thin call, not a parallel implementation, and the shell cannot tell the difference.
    """

    def __init__(
        self,
        *,
        store: PlaybookProposalStore,
        instructions: AgentInstructionsStore,
        playbooks: PlaybookStore,
        tenant: str,
        version: str,
    ) -> None:
        self._store = store
        self._instructions = instructions
        self._playbooks = playbooks
        self._tenant = tenant
        self._version = version

    def manifest(self) -> ModuleManifest:
        """The pseudo-module's manifest — one ``review`` page, no tools, no MCP, no config.

        Declares no ``tools``/``events``/``config``/``secrets`` and leaves ``reindexable`` False:
        the core is not a module and contributes nothing to the agent's tool surface. The ``ui``
        section exists only so the Suggestions inbox has an icon for the group heading.
        """
        return ModuleManifest(
            name=CORE_MODULE_NAME,
            version=self._version,
            description="The agent's own base instructions and named playbooks.",
            pages=[
                PageSpec(
                    id=CORE_REVIEW_PAGE_ID,
                    title="Playbooks",
                    archetype="review",
                    icon="book-open",
                )
            ],
            ui=UiSection(icon="book-open", summary="Agent instructions and playbooks."),
        )

    async def get_page(self, page_id: str) -> dict[str, object]:
        """The ``review`` archetype's data: the pending queue, each with a server-computed diff."""
        self._assert_page(page_id)
        data = await self.list_review()
        return data.model_dump()

    async def list_review(self) -> ReviewData:
        """The pending queue, each proposal diffed against the document it targets.

        ``current`` is the live document (empty for a ``create``) and ``content`` the proposal —
        the two full texts the shell needs to render an editable draft (ADR-0090).
        """
        items: list[ReviewSuggestion] = []
        for p in await self._store.list_pending(tenant=self._tenant):
            current = await self._current_content(p.path, p.operation)
            items.append(
                ReviewSuggestion(
                    id=p.sid,
                    title=_title_for(p.path),
                    path=p.path,
                    operation=p.operation,
                    origin=p.origin,
                    note=p.note,
                    created_at=p.created_at.isoformat(),
                    diff=_unified_diff(p.path, current, p.proposed_content),
                    current=current,
                    content=p.proposed_content,
                )
            )
        return ReviewData(title="Playbooks", suggestions=items)

    async def review_action(
        self, page_id: str, suggestion_id: str, action: str, content: str | None = None
    ) -> dict[str, object]:
        """Approve (apply) or reject (discard) one staged proposal."""
        self._assert_page(page_id)
        if action == "approve":
            result = await self.approve(suggestion_id, content)
        elif action == "reject":
            result = await self.reject(suggestion_id)
        else:
            raise HTTPException(status_code=404, detail=f"unknown review action: {action}")
        return result.model_dump()

    async def review_audit(self, page_id: str, *, limit: int = 50) -> dict[str, object]:
        """The resolved-decision trail for this page, newest first (ADR-0090)."""
        self._assert_page(page_id)
        decisions = await self._store.decisions(tenant=self._tenant, limit=limit)
        return ReviewAuditData(decisions=decisions).model_dump()

    async def approve(self, sid: str, content: str | None = None) -> ApplyResult:
        """Apply a proposal through the storage, then drop it from the queue.

        *content* is the operator's edited result when they revised the draft before approving
        (ADR-0090) — what is applied is what they actually saw and okayed, never the raw
        proposal. Records the audit row (proposal alongside what was applied) **before** the
        pending row drops.
        """
        p = await self._store.get(tenant=self._tenant, sid=sid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"no such suggestion: {sid}")
        applied = p.proposed_content if content is None else content
        await self._apply(p, applied)
        await self._store.record(
            tenant=self._tenant, proposal=p, decision="approved", applied_content=applied
        )
        await self._store.delete(tenant=self._tenant, sid=sid)
        return ApplyResult(id=sid, status="approved", path=p.path, operation=p.operation)

    async def reject(self, sid: str) -> ApplyResult:
        """Discard a proposal, recording the rejection.

        The trail is what makes a rejection more than a delete: ADR-0093 §6 feeds recently
        rejected proposals back to the reflection pass as negative context, so a declined idea
        isn't re-proposed unchanged.
        """
        p = await self._store.get(tenant=self._tenant, sid=sid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"no such suggestion: {sid}")
        await self._store.record(tenant=self._tenant, proposal=p, decision="rejected")
        await self._store.delete(tenant=self._tenant, sid=sid)
        return ApplyResult(id=sid, status="rejected", path=p.path, operation=p.operation)

    async def _apply(self, p: PlaybookProposal, content: str) -> None:
        """Write *content* through the store that owns the document *p* targets (ADR-0093 §3).

        The base instructions go through the **existing** ``AgentInstructionsStore`` — the very
        path the operator's own Settings edit uses, so an approved edit is indistinguishable from
        a hand-typed one and inherits its snapshot-on-save undo.
        """
        name = playbook_name_from_path(p.path)
        if name is None:
            if p.path != INSTRUCTIONS_PATH:
                raise HTTPException(status_code=400, detail=f"unknown target path: {p.path!r}")
            await self._instructions.set_instructions(self._tenant, content)
            return
        existing = await self._playbooks.get_by_name(self._tenant, name)
        if p.operation == "create":
            # A playbook created by hand (or by an earlier approval) between staging and approval
            # turns this into an update — applying the operator's intent beats failing on a race.
            if existing is None:
                await self._playbooks.create(self._tenant, name=name, content=content)
            else:
                await self._playbooks.save(self._tenant, existing.id, content=content)
            return
        if existing is None:
            raise HTTPException(
                status_code=409,
                detail=f"playbook {name!r} no longer exists; reject this proposal instead",
            )
        await self._playbooks.save(self._tenant, existing.id, content=content)

    async def _current_content(self, path: str, operation: str) -> str:
        """The live content of the document *path* names, or ``""`` when it doesn't exist yet."""
        if operation == "create":
            return ""
        name = playbook_name_from_path(path)
        if name is None:
            return await self._instructions.get_base(self._tenant)
        existing = await self._playbooks.get_by_name(self._tenant, name)
        return existing.content if existing is not None else ""

    @staticmethod
    def _assert_page(page_id: str) -> None:
        if page_id != CORE_REVIEW_PAGE_ID:
            raise HTTPException(
                status_code=404, detail=f"module {CORE_MODULE_NAME!r} has no page {page_id!r}"
            )
