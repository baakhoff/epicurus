"""Suggested knowledge changes — staged for operator review, not applied directly (ADR-0033, #220).

Agent-initiated vault edits never touch the vault. Instead the ``knowledge_propose_edit``
tool stages a **suggestion** here; the operator reviews a diff in the *Suggestions* page
(a core-rendered ``review`` archetype) and approves or rejects it. Only an approved
suggestion is written to the vault and indexed. Direct **operator** edits (the editor
save, the file-tree CRUD of #216) stay immediate — the operator is the approver, so
gating their own edits would be pointless. The trust boundary is the author: agent →
review; operator → immediate.

This module owns:

* the tenant-scoped ``knowledge_suggestions`` table + :class:`SuggestionStore` (CRUD),
* :class:`SuggestionReview`, which renders the queue (with a server-computed unified
  diff per suggestion) and applies/discards on approve/reject,
* the HTTP surface the core proxies for the ``review`` page (ADR-0018):

  * ``GET /pages/review`` — the pending queue (``ReviewData``),
  * ``POST /pages/review/suggestions/{id}/approve`` — apply + index, then drop the row,
  * ``POST /pages/review/suggestions/{id}/reject`` — discard the row.

Approve/reject are **not** MCP tools — exposing them to the agent would let it approve
its own proposals, defeating the gate. They are operator-only endpoints the shell calls.
"""

from __future__ import annotations

import difflib
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, String, Text, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import get_logger
from epicurus_core.db import ensure_columns
from epicurus_knowledge.indexer import KnowledgeIndexer
from epicurus_knowledge.pages import VaultPages
from epicurus_knowledge.refs import doc_title, safe_relative

log = get_logger("knowledge.suggestions")

# The review page id this module declares (see service.py manifest `pages`).
REVIEW_PAGE_ID = "review"

# The operations a suggestion may carry (#KB-refactor — the agent's structural tools all
# stage suggestions, never writing directly):
#   create / update — carry proposed document content;
#   delete          — removes a document (no content);
#   move            — move/rename a file or folder (``path`` → ``to_path``);
#   mkdir           — create a folder (at ``path``);
#   mkproject       — create a knowledge base (``path`` is its name).
_OPERATIONS = frozenset({"create", "update", "delete", "move", "mkdir", "mkproject"})
# Operations whose review shows a content diff; the rest are simple confirmations.
_DIFF_OPERATIONS = frozenset({"create", "update", "delete"})

# Columns added to knowledge_suggestions after its first release; added in place at init
# (the store uses ``create_all``, no migration tool) — mirrors storage_files' pattern.
_ADDED_COLUMNS = ("to_path",)


class Suggestion:
    """A single staged change — an immutable value object returned by the store."""

    __slots__ = (
        "created_at",
        "note",
        "operation",
        "origin",
        "path",
        "proposed_content",
        "sid",
        "to_path",
    )

    def __init__(
        self,
        sid: str,
        path: str,
        operation: str,
        proposed_content: str,
        origin: str,
        note: str,
        created_at: datetime,
        to_path: str = "",
    ) -> None:
        self.sid = sid
        self.path = path
        self.operation = operation
        self.proposed_content = proposed_content
        self.origin = origin
        self.note = note
        self.created_at = created_at
        # The destination for a ``move`` (empty for every other operation).
        self.to_path = to_path


# ── persistence ──────────────────────────────────────────────────────────────


class _SuggestionBase(DeclarativeBase):
    pass


class _StoredSuggestion(_SuggestionBase):
    """ORM mapping for one pending suggested change (tenant-scoped)."""

    __tablename__ = "knowledge_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    sid: Mapped[str] = mapped_column(String(32), index=True)
    path: Mapped[str] = mapped_column(String(4096))
    operation: Mapped[str] = mapped_column(String(16))
    proposed_content: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(64), default="agent")
    note: Mapped[str] = mapped_column(Text, default="")
    # Destination path for a ``move`` operation; empty for all others (#KB-refactor).
    # ``server_default`` is raw SQL, hence the quoted empty-string literal — so a freshly
    # created column and the additive reconcile's ``DEFAULT ''`` agree (the bare ``""`` it
    # carried before rendered no default at all).
    to_path: Mapped[str] = mapped_column(String(4096), server_default="''", default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SuggestionStore:
    """CRUD for the tenant-scoped suggestion queue in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_SuggestionBase.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Reconcile columns added after first release via the shared additive helper (#249).

        ``to_path`` (#220 move support) carries a ``server_default`` of ``''``, so the helper
        adds it ``NOT NULL DEFAULT ''`` — backfilling existing rows. See
        :func:`epicurus_core.db.ensure_columns`.
        """
        ensure_columns(sync_conn, _StoredSuggestion.__table__, _ADDED_COLUMNS)

    async def add(
        self,
        *,
        tenant: str,
        path: str,
        operation: str,
        proposed_content: str,
        origin: str,
        note: str,
        to_path: str = "",
    ) -> Suggestion:
        """Stage a new suggestion and return it (with its freshly minted ``sid``)."""
        sid = uuid.uuid4().hex
        async with self._session() as session:
            row = _StoredSuggestion(
                tenant=tenant,
                sid=sid,
                path=path,
                operation=operation,
                proposed_content=proposed_content,
                origin=origin,
                note=note,
                to_path=to_path,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_value(row)

    async def list(self, *, tenant: str) -> list[Suggestion]:
        """All pending suggestions for *tenant*, oldest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredSuggestion)
                .where(_StoredSuggestion.tenant == tenant)
                .order_by(_StoredSuggestion.created_at, _StoredSuggestion.id)
            )
            return [_to_value(row) for row in rows]

    async def get(self, *, tenant: str, sid: str) -> Suggestion | None:
        """One suggestion by its opaque ``sid``, or ``None`` if not found."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredSuggestion).where(
                    _StoredSuggestion.tenant == tenant,
                    _StoredSuggestion.sid == sid,
                )
            )
            return _to_value(row) if row is not None else None

    async def delete(self, *, tenant: str, sid: str) -> bool:
        """Remove a suggestion (on approve-applied or reject). True if a row was deleted."""
        async with self._session() as session:
            row = await session.scalar(
                select(_StoredSuggestion).where(
                    _StoredSuggestion.tenant == tenant,
                    _StoredSuggestion.sid == sid,
                )
            )
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True


def _to_value(row: _StoredSuggestion) -> Suggestion:
    return Suggestion(
        sid=row.sid,
        path=row.path,
        operation=row.operation,
        proposed_content=row.proposed_content,
        origin=row.origin,
        note=row.note,
        created_at=row.created_at,
        to_path=row.to_path or "",
    )


# ── review payloads (the `review` archetype data shape) ───────────────────────


class ReviewSuggestion(BaseModel):
    """One pending change in the review queue, with a server-computed unified diff."""

    id: str
    title: str
    path: str
    operation: str  # create | update | delete | move | mkdir | mkproject
    origin: str
    note: str = ""
    created_at: str  # ISO-8601
    diff: str  # unified diff (current content → proposed); empty for non-content ops
    to_path: str = ""  # destination for a ``move`` (empty otherwise)
    # Full texts so the shell can render a per-hunk review (#KB-refactor): ``current`` is
    # the live document (empty for a create), ``content`` is the proposal (empty for delete).
    current: str = ""
    content: str = ""


class ReviewData(BaseModel):
    """The ``review`` archetype's data: the queue of pending suggestions (ADR-0018)."""

    title: str = "Suggestions"
    suggestions: list[ReviewSuggestion] = Field(default_factory=list)


class ApplyResult(BaseModel):
    """The outcome of approving or rejecting a suggestion."""

    id: str
    status: str  # "approved" | "rejected"
    path: str
    operation: str
    indexed: bool = False


class ApproveBody(BaseModel):
    """Optional approve payload: the operator's edited content for a partial (per-hunk)
    approval of an ``update``/``create``. Absent ⇒ apply the agent's full proposal."""

    content: str | None = None


# ── review orchestration ──────────────────────────────────────────────────────


def _unified_diff(path: str, before: str, after: str) -> str:
    """A unified diff from *before* to *after*, labelled with *path*."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


class SuggestionReview:
    """Renders the review queue and applies/discards suggestions on the operator's word."""

    def __init__(
        self,
        store: SuggestionStore,
        pages: VaultPages,
        indexer: KnowledgeIndexer,
        *,
        vault_path: Path,
        tenant: str,
        read_only: bool = False,
    ) -> None:
        self._store = store
        self._pages = pages
        self._indexer = indexer
        self._vault = vault_path
        self._tenant = tenant
        # Watch mode (#232, ADR-0035): the vault is externally owned, so an approval —
        # which would write the vault — is refused. The agent may still *propose* (no
        # write) and the operator may still reject to clear the queue; only the apply is
        # blocked. The operator makes the change in Obsidian instead.
        self._read_only = read_only

    def _current_content(self, rel: str) -> str:
        """The doc's current vault content, or ``""`` if it does not exist yet."""
        target = safe_relative(self._vault, rel)
        if not target.is_file():
            return ""
        return target.read_text(encoding="utf-8", errors="replace")

    async def list_review(self) -> ReviewData:
        """The pending queue, each content change paired with a diff against the live vault.

        Content operations (create/update/delete) carry a unified diff; structural ones
        (move/mkdir/mkproject) carry an empty diff — the shell reviews them as a simple
        confirmation from ``path`` / ``to_path`` (#KB-refactor).
        """
        items: list[ReviewSuggestion] = []
        for s in await self._store.list(tenant=self._tenant):
            diff = ""
            current = ""
            content = ""
            if s.operation in _DIFF_OPERATIONS:
                current = self._current_content(s.path)
                content = "" if s.operation == "delete" else s.proposed_content
                diff = _unified_diff(s.path, current, content)
            items.append(
                ReviewSuggestion(
                    id=s.sid,
                    title=doc_title(s.to_path or s.path),
                    path=s.path,
                    operation=s.operation,
                    origin=s.origin,
                    note=s.note,
                    created_at=s.created_at.isoformat(),
                    diff=diff,
                    to_path=s.to_path,
                    current=current,
                    content=content,
                )
            )
        return ReviewData(suggestions=items)

    async def _reindex_move(self, from_rel: str, to_rel: str) -> bool:
        """Re-index after a move: a single file swaps its vectors; a folder reconciles fully."""
        if from_rel.endswith(".md"):
            await self._indexer.remove_path(from_rel)
            try:
                await self._indexer.index_path(to_rel)
                return True
            except Exception as exc:
                log.warning("move applied but re-index failed", path=to_rel, error=str(exc))
                return False
        # A folder move renames many note paths — let the incremental indexer reconcile.
        try:
            await self._indexer.run()
            return True
        except Exception as exc:
            log.warning("folder move applied but re-index failed", path=to_rel, error=str(exc))
            return False

    async def approve(self, sid: str, content: str | None = None) -> ApplyResult:
        """Apply a suggestion to the vault (and index it), then drop it from the queue.

        404 if the suggestion is unknown. ``create``/``update`` write the proposed content
        — or *content*, when the operator approved only part of an edit (per-hunk review,
        #KB-refactor) — and re-index the file; ``delete`` removes the file and its vectors;
        ``move`` relocates a file/folder and re-indexes; ``mkdir`` / ``mkproject`` create a
        folder / knowledge base.

        409 when the vault is externally owned (watch mode, #232): applying would write a
        vault Obsidian owns, so the apply is refused. Reject still works to clear the queue.
        """
        if self._read_only:
            raise HTTPException(
                status_code=409,
                detail=(
                    "cannot apply suggestions while a watched external vault is mounted"
                    " (VAULT_WATCH): the vault is managed in Obsidian — make the change there"
                ),
            )
        s = await self._store.get(tenant=self._tenant, sid=sid)
        if s is None:
            raise HTTPException(status_code=404, detail=f"no such suggestion: {sid}")
        indexed = False
        if s.operation in ("create", "update"):
            body = s.proposed_content if content is None else content
            result = await self._pages.write_doc(s.path, body)
            indexed = result.indexed
        elif s.operation == "delete":
            # Delete through the core file API (ADR-0064); an already-gone file (404) is fine.
            try:
                await self._pages.delete_doc(s.path)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            await self._indexer.remove_path(s.path)
        elif s.operation == "move":
            await self._pages.move_item(s.path, s.to_path)
            indexed = await self._reindex_move(s.path, s.to_path)
        elif s.operation == "mkdir":
            await self._pages.create_folder(s.path)
        elif s.operation == "mkproject":
            await self._pages.create_project(s.path)
        else:  # defensive — propose validates, but never trust stored data blindly
            raise HTTPException(status_code=400, detail=f"unknown operation: {s.operation}")
        await self._store.delete(tenant=self._tenant, sid=sid)
        log.info("suggestion approved", sid=sid, operation=s.operation, path=s.path)
        return ApplyResult(
            id=sid,
            status="approved",
            path=s.to_path or s.path,
            operation=s.operation,
            indexed=indexed,
        )

    async def reject(self, sid: str) -> ApplyResult:
        """Discard a suggestion without touching the vault. 404 if unknown."""
        s = await self._store.get(tenant=self._tenant, sid=sid)
        if s is None:
            raise HTTPException(status_code=404, detail=f"no such suggestion: {sid}")
        await self._store.delete(tenant=self._tenant, sid=sid)
        log.info("suggestion rejected", sid=sid, operation=s.operation, path=s.path)
        return ApplyResult(id=sid, status="rejected", path=s.path, operation=s.operation)


def validate_operation(operation: str) -> str:
    """Normalise + validate a proposed operation; raise ``ValueError`` if unknown."""
    op = operation.strip().lower()
    if op not in _OPERATIONS:
        raise ValueError(f"operation must be one of {sorted(_OPERATIONS)}, got {operation!r}")
    return op


def create_review_router(review: SuggestionReview) -> APIRouter:
    """The HTTP surface the core proxies for the ``review`` page (ADR-0018, ADR-0033).

    Registered **before** the editor pages router so the literal ``/pages/review`` data
    route is matched ahead of the editor's ``/pages/{page_id}`` path parameter.
    """
    router = APIRouter(tags=["pages"])

    @router.get("/pages/review", response_model=ReviewData)
    async def get_review() -> ReviewData:
        return await review.list_review()

    @router.post("/pages/review/suggestions/{suggestion_id}/approve", response_model=ApplyResult)
    async def approve(suggestion_id: str, body: ApproveBody | None = None) -> ApplyResult:
        # ``body.content`` lets the operator approve only part of an edit (per-hunk review,
        # #KB-refactor); absent ⇒ apply the agent's full proposal.
        return await review.approve(suggestion_id, body.content if body else None)

    @router.post("/pages/review/suggestions/{suggestion_id}/reject", response_model=ApplyResult)
    async def reject(suggestion_id: str) -> ApplyResult:
        return await review.reject(suggestion_id)

    return router


__all__ = [
    "REVIEW_PAGE_ID",
    "ApplyResult",
    "ApproveBody",
    "ReviewData",
    "ReviewSuggestion",
    "Suggestion",
    "SuggestionReview",
    "SuggestionStore",
    "create_review_router",
    "validate_operation",
]
