"""The chat-attachment surface the notes module exposes (ADR-0019, #134).

Notes are **attach-only**: the agent has no retrieval tool over them and cannot read
a note unless the user explicitly attaches it to a turn. That makes this surface the
*only* path from a note into the agent's context. The core proxies:

* ``GET /attachments`` — the picker: every note as ``{ref_id, kind, title}``.
* ``GET /attachments/{ref_id}`` — resolve: the note's body, which the core injects
  into the turn as the attachment's text block.

``ref_id`` is the note slug. Content is read straight from Postgres (the source of
truth) — the vector index is not consulted here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from epicurus_notes.db import NotesStore

ATTACHMENT_KIND = "note"


class AttachmentItem(BaseModel):
    """One pickable note in the attachment picker."""

    ref_id: str
    kind: str = ATTACHMENT_KIND
    title: str


class AttachmentContent(BaseModel):
    """A resolved note's content, injected into the agent turn."""

    title: str
    excerpt: str


class NotesAttachments:
    """Serves the attachment picker + resolve from the notes store."""

    def __init__(self, store: NotesStore, *, tenant: str) -> None:
        self._store = store
        self._tenant = tenant

    async def list_items(self) -> list[AttachmentItem]:
        """Every note for the tenant, as attachable items (newest first)."""
        summaries = await self._store.list_summaries(tenant=self._tenant)
        return [AttachmentItem(ref_id=s.slug, title=s.title) for s in summaries]

    async def resolve(self, ref_id: str) -> AttachmentContent:
        """The note's body for the agent to read. 404 if it does not exist."""
        note = await self._store.get(tenant=self._tenant, slug=ref_id)
        if note is None:
            raise HTTPException(status_code=404, detail=f"no such note: {ref_id}")
        return AttachmentContent(title=note.title, excerpt=note.content)


def create_attachments_router(attachments: NotesAttachments) -> APIRouter:
    """The HTTP surface the core proxies for chat attachments (ADR-0019)."""
    router = APIRouter(tags=["attachments"])

    @router.get("/attachments", response_model=list[AttachmentItem])
    async def list_attachments() -> list[AttachmentItem]:
        return await attachments.list_items()

    @router.get("/attachments/{ref_id}", response_model=AttachmentContent)
    async def resolve_attachment(ref_id: str) -> AttachmentContent:
        return await attachments.resolve(ref_id)

    return router
