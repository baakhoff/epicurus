"""Knowledge as a chat **attachment source** (#137, ADR-0019).

The user can attach a vault document to a chat turn so the agent uses it as explicit
context, beyond default retrieval. The module supplies data only — the core's
``AttachMenu`` renders the picker and the ``AttachmentExpander`` injects the resolved
text into the turn:

* ``GET /attachments`` — the picker: every vault document, as ``{ref_id, kind, title}``.
* ``GET /attachments/{ref_id}`` — the resolve: one document's text.

Only the operator's vault is attachable — its notes are the operator's own context.
The bundled platform docs reach the agent through retrieval (``knowledge_search``), not
the picker, so they are deliberately absent here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from epicurus_knowledge.reader import VaultReader
from epicurus_knowledge.refs import (
    KNOWLEDGE_KIND,
    SOURCE_NOTE,
    decode_ref,
    doc_title,
    encode_ref,
    safe_vault_rel,
)


class AttachmentItem(BaseModel):
    """One pickable document offered by the attachment picker."""

    ref_id: str
    kind: str = KNOWLEDGE_KIND
    title: str


class AttachmentContent(BaseModel):
    """A resolved attachment: the document's text for the agent to read."""

    title: str
    path: str
    text: str


class VaultAttachments:
    """Serves the attachment picker and resolve from the operator's vault.

    Reads through a :class:`~reader.VaultReader` (the core file API by default, #346) so the
    picker needs no ``/data`` mount.
    """

    def __init__(self, reader: VaultReader) -> None:
        self._reader = reader

    async def list(self) -> list[AttachmentItem]:
        """Every vault document as a pickable item (sorted by path, like the editor)."""
        return [
            AttachmentItem(ref_id=encode_ref(SOURCE_NOTE, rel), title=doc_title(rel))
            for rel in await self._reader.md_files()
        ]

    async def read(self, ref_id: str) -> AttachmentContent:
        """The text of the vault document *ref_id* names. 400 on a bad path, 404 if absent."""
        source, path = decode_ref(ref_id)
        if source != SOURCE_NOTE:
            # Only vault notes are attachable; a doc-source id never came from this picker.
            raise HTTPException(status_code=404, detail="not an attachable document")
        text = await self._reader.read_text(safe_vault_rel(path))
        if text is None:
            raise HTTPException(status_code=404, detail=f"no such document: {path}")
        return AttachmentContent(title=doc_title(path), path=path, text=text)


def create_attachments_router(attachments: VaultAttachments) -> APIRouter:
    """The attachment surface the core proxies (#137, ADR-0019)."""
    router = APIRouter(tags=["attachments"])

    @router.get("/attachments", response_model=list[AttachmentItem])
    async def list_attachments() -> list[AttachmentItem]:
        return await attachments.list()

    @router.get("/attachments/{ref_id}", response_model=AttachmentContent)
    async def resolve_attachment(ref_id: str) -> AttachmentContent:
        return await attachments.read(ref_id)

    return router
