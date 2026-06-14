"""The ``editor`` page the knowledge module contributes (ADR-0018, #130).

The web shell renders an Obsidian-like editor from the bounded core vocabulary; this
module supplies **data only** over three endpoints the core proxies:

* ``GET /pages/{page_id}`` — the browsable document list (``EditorData``).
* ``GET /pages/{page_id}/doc?path=<rel>`` — one document's content (``EditorDocContent``).
* ``PUT /pages/{page_id}/doc?path=<rel>`` — save a document, then re-index just that
  file so the vault stays agent-retrievable (contrast Notes, which is attach-only).

No markup is served — the shell owns all chrome. ``path`` is vault-relative and strictly
confined to the vault root by :func:`~epicurus_knowledge.refs.safe_relative` (no
traversal, ``.md`` only): the editor writes real files, so this is the security boundary.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from epicurus_core import get_logger
from epicurus_knowledge.indexer import KnowledgeIndexer
from epicurus_knowledge.refs import doc_title, iter_md_files, safe_relative

log = get_logger("knowledge.pages")

# The single editor page id this module declares (see service.py manifest `pages`).
VAULT_PAGE_ID = "vault"


class EditorDoc(BaseModel):
    """One entry in the editor's document list."""

    id: str
    title: str
    path: str


class EditorData(BaseModel):
    """The ``editor`` archetype's list payload — the browsable set of documents."""

    title: str = "Knowledge"
    docs: list[EditorDoc] = Field(default_factory=list)


class EditorDocContent(BaseModel):
    """One document's full content, returned when the editor opens it."""

    path: str
    title: str
    content: str


class DocBody(BaseModel):
    """The save request body: the document's full new content."""

    content: str


class EditorSaveResult(BaseModel):
    """The outcome of a save: the path, and whether the re-index succeeded."""

    path: str
    indexed: bool
    chunk_count: int = 0


class VaultPages:
    """Serves the editor page's data from the operator's Obsidian vault."""

    def __init__(self, vault_path: Path, indexer: KnowledgeIndexer) -> None:
        self._vault = vault_path
        self._indexer = indexer

    def list_docs(self) -> EditorData:
        """Every ``.md`` document in the vault, by relative path (sorted)."""
        docs = [
            EditorDoc(id=rel, title=doc_title(rel), path=rel) for rel in iter_md_files(self._vault)
        ]
        return EditorData(docs=docs)

    def read_doc(self, rel: str) -> EditorDocContent:
        """One document's content. 404 if it does not exist."""
        target = safe_relative(self._vault, rel)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"no such document: {rel}")
        content = target.read_text(encoding="utf-8", errors="replace")
        return EditorDocContent(path=rel, title=doc_title(rel), content=content)

    async def write_doc(self, rel: str, content: str) -> EditorSaveResult:
        """Write a document (creating it if new), then re-index just that file.

        The file is the source of truth and is saved first; if the re-index embed
        round-trip fails (e.g. the core is paused), the save still succeeds with
        ``indexed=False`` so an edit is never lost — the Re-index action can retry.
        """
        target = safe_relative(self._vault, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        try:
            chunk_count = await self._indexer.index_path(rel)
        except Exception as exc:  # the edit is saved; indexing is best-effort here
            log.warning("save succeeded but re-index failed", path=rel, error=str(exc))
            return EditorSaveResult(path=rel, indexed=False)
        return EditorSaveResult(path=rel, indexed=True, chunk_count=chunk_count)


def create_pages_router(pages: VaultPages) -> APIRouter:
    """The HTTP surface the core proxies for the editor page (ADR-0018)."""
    router = APIRouter(tags=["pages"])

    def _require_known_page(page_id: str) -> None:
        if page_id != VAULT_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no such page: {page_id}")

    @router.get("/pages/{page_id}", response_model=EditorData)
    async def get_page(page_id: str) -> EditorData:
        _require_known_page(page_id)
        return pages.list_docs()

    @router.get("/pages/{page_id}/doc", response_model=EditorDocContent)
    async def get_doc(page_id: str, path: str = Query(...)) -> EditorDocContent:
        _require_known_page(page_id)
        return pages.read_doc(path)

    @router.put("/pages/{page_id}/doc", response_model=EditorSaveResult)
    async def put_doc(page_id: str, body: DocBody, path: str = Query(...)) -> EditorSaveResult:
        _require_known_page(page_id)
        return await pages.write_doc(path, body.content)

    return router
