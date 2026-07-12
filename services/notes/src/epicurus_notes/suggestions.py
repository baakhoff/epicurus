"""Suggested note changes — staged for operator review, not applied directly.

Notes are **private**: the agent has no read access to a note's body (no get/read tool,
and the `.md` mirror is hidden from its file tools). But it may still *propose* changes —
create, edit (full replace), append, or delete — exactly like the knowledge base
(ADR-0033). Each proposal is staged here; the operator reviews it in the same overlay
(the chat composer bubble + the Suggestions page) and approves/rejects. Only on approval
is the note written (via :class:`~epicurus_notes.pages.NotesPages`) and indexed.

The shape mirrors the knowledge review surface so the core's cross-module feed
(`GET /platform/v1/suggestions`) and the shared web overlay render notes suggestions with
no special-casing. Notes are slug-keyed in Postgres (no filesystem), so there is no
``move``/folder operation; ``append`` is notes-specific — the agent supplies only the text
to add (it cannot read the note), and the server concatenates it onto the current body.
"""

from __future__ import annotations

import difflib
import uuid
from datetime import datetime

from fastapi import APIRouter
from sqlalchemy import DateTime, String, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import get_logger
from epicurus_core.review import (
    ApplyResult,
    ApproveBody,
    ReviewAuditData,
    ReviewData,
    ReviewDecision,
    ReviewSuggestion,
)
from epicurus_notes.db import NotesStore
from epicurus_notes.pages import NotesPages, derive_title

log = get_logger("notes.suggestions")

# The review page id this module declares (see service.py manifest `pages`).
REVIEW_PAGE_ID = "review"

# create/update carry a full proposed body; append carries the text to add; delete none.
_OPERATIONS = frozenset({"create", "update", "append", "delete"})


class NoteSuggestion:
    """A single staged note change — an immutable value object returned by the store."""

    __slots__ = ("created_at", "note", "operation", "origin", "proposed_content", "sid", "slug")

    def __init__(
        self,
        sid: str,
        slug: str,
        operation: str,
        proposed_content: str,
        origin: str,
        note: str,
        created_at: datetime,
    ) -> None:
        self.sid = sid
        self.slug = slug
        self.operation = operation
        self.proposed_content = proposed_content
        self.origin = origin
        self.note = note
        self.created_at = created_at


# ── persistence ──────────────────────────────────────────────────────────────


class _NoteSuggestionBase(DeclarativeBase):
    pass


class _StoredNoteSuggestion(_NoteSuggestionBase):
    """ORM mapping for one pending suggested note change (tenant-scoped)."""

    __tablename__ = "notes_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    sid: Mapped[str] = mapped_column(String(32), index=True)
    slug: Mapped[str] = mapped_column(String(512))
    operation: Mapped[str] = mapped_column(String(16))
    proposed_content: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(64), default="agent")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NoteSuggestionStore:
    """CRUD for the tenant-scoped note-suggestion queue in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_NoteSuggestionBase.metadata.create_all)

    async def add(
        self,
        *,
        tenant: str,
        slug: str,
        operation: str,
        proposed_content: str,
        origin: str,
        note: str,
    ) -> NoteSuggestion:
        sid = uuid.uuid4().hex
        async with self._session() as session:
            row = _StoredNoteSuggestion(
                tenant=tenant,
                sid=sid,
                slug=slug,
                operation=operation,
                proposed_content=proposed_content,
                origin=origin,
                note=note,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_value(row)

    async def list(self, *, tenant: str) -> list[NoteSuggestion]:
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredNoteSuggestion)
                .where(_StoredNoteSuggestion.tenant == tenant)
                .order_by(_StoredNoteSuggestion.created_at, _StoredNoteSuggestion.id)
            )
            return [_to_value(r) for r in rows]

    async def get(self, *, tenant: str, sid: str) -> NoteSuggestion | None:
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredNoteSuggestion).where(
                    _StoredNoteSuggestion.tenant == tenant,
                    _StoredNoteSuggestion.sid == sid,
                )
            )
            return _to_value(row) if row is not None else None

    async def delete(self, *, tenant: str, sid: str) -> bool:
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredNoteSuggestion).where(
                    _StoredNoteSuggestion.tenant == tenant,
                    _StoredNoteSuggestion.sid == sid,
                )
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True


def _to_value(row: _StoredNoteSuggestion) -> NoteSuggestion:
    return NoteSuggestion(
        sid=row.sid,
        slug=row.slug,
        operation=row.operation,
        proposed_content=row.proposed_content,
        origin=row.origin,
        note=row.note,
        created_at=row.created_at,
    )


# ── review payloads (the `review` archetype data shape, ADR-0090) ─────────────
#
# ReviewSuggestion / ReviewData / ApplyResult / ApproveBody / ReviewDecision /
# ReviewAuditData are the shared epicurus-core contract (imported above) — the same shapes
# knowledge's review page uses, so the core's cross-module feed and the shared web overlay
# render notes suggestions with no special-casing (they used to be copy-pasted locally).


# ── suggestion audit trail (ADR-0090) ──────────────────────────────────────────

# Per-tenant retention cap, mirroring the editor version-history MAX_VERSIONS (ADR-0046).
MAX_DECISIONS = 200


class _NoteAuditBase(DeclarativeBase):
    pass


class _StoredNoteDecision(_NoteAuditBase):
    """An immutable audit row for one resolved note suggestion (tenant-scoped, ADR-0090).

    Recorded before the pending row is dropped, pairing what was proposed with what the
    operator actually approved (including any edit).
    """

    __tablename__ = "notes_suggestion_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    sid: Mapped[str] = mapped_column(String(32))
    slug: Mapped[str] = mapped_column(String(512))
    operation: Mapped[str] = mapped_column(String(16))
    origin: Mapped[str] = mapped_column(String(64), default="agent")
    note: Mapped[str] = mapped_column(Text, default="")
    proposed_content: Mapped[str] = mapped_column(Text, default="")
    applied_content: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(16))
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NoteSuggestionAuditStore:
    """An append-only, capped audit trail of resolved note suggestions (ADR-0090).

    Mirrors :class:`epicurus_knowledge.suggestions.SuggestionAuditStore` — one row per
    approve/reject, retained up to ``MAX_DECISIONS`` per tenant.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_NoteAuditBase.metadata.create_all)

    async def record(
        self,
        *,
        tenant: str,
        sid: str,
        slug: str,
        operation: str,
        origin: str,
        note: str,
        proposed_at: datetime,
        decision: str,
        proposed_content: str,
        applied_content: str,
    ) -> None:
        """Append one decision row, then prune anything past the retention cap."""
        async with self._session() as session:
            session.add(
                _StoredNoteDecision(
                    tenant=tenant,
                    sid=sid,
                    slug=slug,
                    operation=operation,
                    origin=origin,
                    note=note,
                    proposed_content=proposed_content,
                    applied_content=applied_content,
                    decision=decision,
                    proposed_at=proposed_at,
                )
            )
            await session.commit()
            stale_ids = list(
                await session.scalars(
                    select(_StoredNoteDecision.id)
                    .where(_StoredNoteDecision.tenant == tenant)
                    .order_by(_StoredNoteDecision.id.desc())
                    .offset(MAX_DECISIONS)
                )
            )
            if stale_ids:
                await session.execute(
                    delete(_StoredNoteDecision).where(_StoredNoteDecision.id.in_(stale_ids))
                )
                await session.commit()

    async def list(self, *, tenant: str, limit: int = 50) -> list[ReviewDecision]:
        """The newest *limit* resolved decisions for *tenant*, most recent first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredNoteDecision)
                .where(_StoredNoteDecision.tenant == tenant)
                .order_by(_StoredNoteDecision.id.desc())
                .limit(limit)
            )
            return [
                ReviewDecision(
                    id=row.sid,
                    title=row.slug,
                    path=row.slug,
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


def _unified_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


def _compose(operation: str, current: str, proposed: str) -> str:
    """The full body that approving this operation would produce.

    create/update replace with *proposed*; ``append`` concatenates *proposed* onto the
    current body (the agent supplies only the text to add, since it cannot read the note);
    ``delete`` yields the empty string.
    """
    if operation == "delete":
        return ""
    if operation == "append":
        return f"{current}\n{proposed}" if current.strip() else proposed
    return proposed


class NoteSuggestionReview:
    """Renders the note-suggestion queue and applies/discards on the operator's word."""

    def __init__(
        self,
        store: NoteSuggestionStore,
        pages: NotesPages,
        notes: NotesStore,
        *,
        tenant: str,
        audit: NoteSuggestionAuditStore,
    ) -> None:
        self._store = store
        self._pages = pages
        self._notes = notes
        self._tenant = tenant
        # Resolved-decision audit trail (ADR-0090) — recorded before a row leaves the queue.
        self._audit = audit

    async def _current_content(self, slug: str) -> str:
        note = await self._notes.get(tenant=self._tenant, slug=slug)
        return note.content if note is not None else ""

    async def list_review(self) -> ReviewData:
        items: list[ReviewSuggestion] = []
        for s in await self._store.list(tenant=self._tenant):
            current = await self._current_content(s.slug)
            content = _compose(s.operation, current, s.proposed_content)
            items.append(
                ReviewSuggestion(
                    id=s.sid,
                    title=s.slug,
                    path=s.slug,
                    operation=s.operation,
                    origin=s.origin,
                    note=s.note,
                    created_at=s.created_at.isoformat(),
                    diff=_unified_diff(s.slug, current, content),
                    current=current,
                    content=content,
                )
            )
        return ReviewData(suggestions=items)

    async def approve(self, sid: str, content: str | None = None) -> ApplyResult:
        """Apply a staged note change, then drop it from the queue. 404 if unknown.

        Records an audit row (ADR-0090) — the proposal alongside what was actually
        applied, including any operator edit — before the pending row drops.
        """
        from fastapi import HTTPException

        s = await self._store.get(tenant=self._tenant, sid=sid)
        if s is None:
            raise HTTPException(status_code=404, detail=f"no such suggestion: {sid}")
        indexed = False
        applied_content = ""
        if s.operation == "delete":
            await self._pages.delete_doc(s.slug)
        else:
            # Honour the operator's edited content (ADR-0090); else compose from the
            # current body.
            if content is None:
                current = await self._current_content(s.slug)
                content = _compose(s.operation, current, s.proposed_content)
            applied_content = content
            result = await self._pages.write_doc(s.slug, content)
            indexed = result.indexed
        await self._audit.record(
            tenant=self._tenant,
            sid=sid,
            slug=s.slug,
            operation=s.operation,
            origin=s.origin,
            note=s.note,
            proposed_at=s.created_at,
            decision="approved",
            proposed_content=s.proposed_content,
            applied_content=applied_content,
        )
        await self._store.delete(tenant=self._tenant, sid=sid)
        log.info("note suggestion approved", sid=sid, operation=s.operation, slug=s.slug)
        return ApplyResult(
            id=sid, status="approved", path=s.slug, operation=s.operation, indexed=indexed
        )

    async def reject(self, sid: str) -> ApplyResult:
        """Discard a suggestion without touching the note. 404 if unknown.

        Records an audit row (ADR-0090) so a rejected proposal stays visible in history.
        """
        from fastapi import HTTPException

        s = await self._store.get(tenant=self._tenant, sid=sid)
        if s is None:
            raise HTTPException(status_code=404, detail=f"no such suggestion: {sid}")
        await self._audit.record(
            tenant=self._tenant,
            sid=sid,
            slug=s.slug,
            operation=s.operation,
            origin=s.origin,
            note=s.note,
            proposed_at=s.created_at,
            decision="rejected",
            proposed_content=s.proposed_content,
            applied_content="",
        )
        await self._store.delete(tenant=self._tenant, sid=sid)
        log.info("note suggestion rejected", sid=sid, operation=s.operation, slug=s.slug)
        return ApplyResult(id=sid, status="rejected", path=s.slug, operation=s.operation)

    async def list_audit(self, limit: int = 50) -> ReviewAuditData:
        """The resolved-decision audit trail for this tenant, newest first (ADR-0090)."""
        return ReviewAuditData(decisions=await self._audit.list(tenant=self._tenant, limit=limit))


def validate_note_operation(operation: str) -> str:
    """Normalise + validate a proposed note operation; raise ``ValueError`` if unknown."""
    op = operation.strip().lower()
    if op not in _OPERATIONS:
        raise ValueError(f"operation must be one of {sorted(_OPERATIONS)}, got {operation!r}")
    return op


def create_note_review_router(review: NoteSuggestionReview) -> APIRouter:
    """The HTTP surface the core proxies for the notes ``review`` page (ADR-0018/0033).

    Registered **before** the editor pages router so ``/pages/review`` is matched ahead of
    the editor's ``/pages/{page_id}`` path parameter.
    """
    router = APIRouter(tags=["pages"])

    @router.get("/pages/review", response_model=ReviewData)
    async def get_review() -> ReviewData:
        return await review.list_review()

    @router.post("/pages/review/suggestions/{suggestion_id}/approve", response_model=ApplyResult)
    async def approve(suggestion_id: str, body: ApproveBody | None = None) -> ApplyResult:
        return await review.approve(suggestion_id, body.content if body else None)

    @router.post("/pages/review/suggestions/{suggestion_id}/reject", response_model=ApplyResult)
    async def reject(suggestion_id: str) -> ApplyResult:
        return await review.reject(suggestion_id)

    @router.get("/pages/review/audit", response_model=ReviewAuditData)
    async def get_audit(limit: int = 50) -> ReviewAuditData:
        return await review.list_audit(limit=limit)

    return router


__all__ = [
    "MAX_DECISIONS",
    "REVIEW_PAGE_ID",
    "ApplyResult",
    "ApproveBody",
    "NoteSuggestion",
    "NoteSuggestionAuditStore",
    "NoteSuggestionReview",
    "NoteSuggestionStore",
    "ReviewAuditData",
    "ReviewData",
    "ReviewDecision",
    "ReviewSuggestion",
    "create_note_review_router",
    "derive_title",
    "validate_note_operation",
]
