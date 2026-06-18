"""The ``editor`` page the knowledge module contributes (ADR-0018, #130).

The web shell renders an Obsidian-like editor from the bounded core vocabulary; this
module supplies **data only** over three endpoints the core proxies:

* ``GET /pages/{page_id}`` — the browsable document/folder tree (``EditorData``).
* ``GET /pages/{page_id}/doc?path=<rel>`` — one document's content (``EditorDocContent``).
* ``PUT /pages/{page_id}/doc?path=<rel>`` — save a document, then re-index just that
  file so the vault stays agent-retrievable (contrast Notes, which is attach-only).
* ``POST /pages/{page_id}/folder?path=<rel>`` — create a directory; 409 if it already exists.
* ``DELETE /pages/{page_id}/doc?path=<rel>`` — delete a ``.md`` file; 404 if absent.
* ``DELETE /pages/{page_id}/folder?path=<rel>`` — delete an empty directory; 409 if not empty.
* ``POST /pages/{page_id}/move`` — move/rename a file or folder; 409 on collision.

No markup is served — the shell owns all chrome. ``path`` is vault-relative and strictly
confined to the vault root by :func:`~epicurus_knowledge.refs.safe_relative` (no
traversal, ``.md`` only): the editor writes real files, so this is the security boundary.
For folder paths :func:`~epicurus_knowledge.refs.safe_dir_relative` is used instead.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from epicurus_core import get_logger
from epicurus_knowledge.indexer import KnowledgeIndexer
from epicurus_knowledge.refs import (
    doc_title,
    iter_tree_nodes,
    safe_dir_relative,
    safe_relative,
)

log = get_logger("knowledge.pages")

# The single editor page id this module declares (see service.py manifest `pages`).
VAULT_PAGE_ID = "vault"


class EditorDoc(BaseModel):
    """One entry in the editor's document/folder tree."""

    id: str
    title: str
    path: str
    type: str = "file"  # "file" | "dir"


class EditorData(BaseModel):
    """The ``editor`` archetype's list payload — the browsable document/folder tree."""

    title: str = "Knowledge"
    docs: list[EditorDoc] = Field(default_factory=list)
    can_manage_files: bool = False  # True → the shell shows folder CRUD controls (#216)


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


class MoveBody(BaseModel):
    """Request body for a file/folder move or rename."""

    from_path: str
    to_path: str


class VaultPages:
    """Serves the editor page's data from the operator's Obsidian vault."""

    def __init__(self, vault_path: Path, indexer: KnowledgeIndexer) -> None:
        self._vault = vault_path
        self._indexer = indexer

    def list_docs(self) -> EditorData:
        """Every ``.md`` document and non-hidden subdirectory in the vault (depth-first sorted)."""

        def _title(node: dict[str, str]) -> str:
            if node["type"] == "file":
                return doc_title(node["path"])
            return node["path"].split("/")[-1]

        docs = [
            EditorDoc(id=node["path"], title=_title(node), path=node["path"], type=node["type"])
            for node in iter_tree_nodes(self._vault)
        ]
        return EditorData(docs=docs, can_manage_files=True)

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

    def create_folder(self, rel: str) -> dict[str, str]:
        """Create a directory at *rel* relative to the vault root.

        409 if it already exists, 400 for any path-safety violation.
        """
        target = safe_dir_relative(self._vault, rel)
        if target.exists():
            raise HTTPException(status_code=409, detail=f"folder already exists: {rel}")
        target.mkdir(parents=True, exist_ok=False)
        log.info("folder created", path=rel)
        return {"path": rel}

    def delete_doc(self, rel: str) -> None:
        """Delete a ``.md`` file at *rel* relative to the vault root.

        404 if it does not exist, 400 for any path-safety violation.
        """
        target = safe_relative(self._vault, rel)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"no such document: {rel}")
        target.unlink()
        log.info("document deleted", path=rel)

    def delete_folder(self, rel: str) -> None:
        """Delete an **empty** directory at *rel* relative to the vault root.

        404 if it does not exist, 409 if it is not empty, 400 for any
        path-safety violation.
        """
        target = safe_dir_relative(self._vault, rel)
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"no such folder: {rel}")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail=f"not a directory: {rel}")
        if any(target.iterdir()):
            raise HTTPException(status_code=409, detail=f"folder is not empty: {rel}")
        target.rmdir()
        log.info("folder deleted", path=rel)

    def move_item(self, from_rel: str, to_rel: str) -> dict[str, str]:
        """Move or rename a file or folder within the vault.

        The *from* path is resolved via the appropriate safety check (file or
        directory); the *to* path is always resolved via
        :func:`~epicurus_knowledge.refs.safe_dir_relative` (no ``.md``
        requirement, because both files and directories land here). 404 if the
        source does not exist, 409 if the destination already exists, 400 for
        any path-safety violation.
        """
        from_target = safe_dir_relative(self._vault, from_rel)
        to_target = safe_dir_relative(self._vault, to_rel)
        if not from_target.exists():
            raise HTTPException(status_code=404, detail=f"no such file or folder: {from_rel}")
        if to_target.exists():
            raise HTTPException(status_code=409, detail=f"destination already exists: {to_rel}")
        to_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(from_target), str(to_target))
        log.info("item moved", from_path=from_rel, to_path=to_rel)
        return {"path": to_rel}


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

    @router.post("/pages/{page_id}/folder")
    async def post_folder(page_id: str, path: str = Query(...)) -> dict[str, str]:
        _require_known_page(page_id)
        return pages.create_folder(path)

    @router.delete("/pages/{page_id}/doc", status_code=204)
    async def delete_doc(page_id: str, path: str = Query(...)) -> None:
        _require_known_page(page_id)
        pages.delete_doc(path)

    @router.delete("/pages/{page_id}/folder", status_code=204)
    async def delete_folder(page_id: str, path: str = Query(...)) -> None:
        _require_known_page(page_id)
        pages.delete_folder(path)

    @router.post("/pages/{page_id}/move")
    async def post_move(page_id: str, body: MoveBody) -> dict[str, str]:
        _require_known_page(page_id)
        return pages.move_item(body.from_path, body.to_path)

    return router
