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
from epicurus_knowledge.db import VersionStore
from epicurus_knowledge.indexer import KnowledgeIndexer
from epicurus_knowledge.refs import (
    doc_title,
    iter_projects,
    iter_tree_nodes,
    safe_dir_relative,
    safe_project,
    safe_relative,
)

log = get_logger("knowledge.pages")

# The single editor page id this module declares (see service.py manifest `pages`).
VAULT_PAGE_ID = "vault"

# The reserved, read-only scope that surfaces the bundled platform docs in the editor's
# knowledge-base switcher (#KB-refactor, req 3) — "make a service's documentation visible
# in the knowledge base". The ``_`` prefix can never collide with a real project name
# (see :func:`~epicurus_knowledge.refs.safe_project`), so the path scheme stays unambiguous.
DOCS_SCOPE_ID = "__docs__"
DOCS_SCOPE_TITLE = "Platform docs"

# The noun the shell shows on the scope switcher and its "New …" control. Knowledge sets
# it; Notes leaves it empty (no switcher), keeping the shared editor archetype generic.
SCOPE_NOUN = "knowledge base"

# Surfaced to the shell and raised as HTTP 409 when the vault is externally owned (#232,
# ADR-0035): in watch mode Obsidian (or whatever syncs the folder) is the sole author, so
# epicurus refuses every write — the editor save, the file-tree CRUD, the move/rename — and
# the editor page renders read-only.
VAULT_READ_ONLY_DETAIL = (
    "vault is read-only: a watched external vault is mounted (VAULT_WATCH), so it is managed"
    " in Obsidian (or whatever syncs the folder) — edit it there"
)


class EditorDoc(BaseModel):
    """One entry in the editor's document/folder tree."""

    id: str
    title: str
    path: str
    type: str = "file"  # "file" | "dir"


class EditorScope(BaseModel):
    """One selectable scope in the editor's switcher (#KB-refactor).

    A ``project`` is a writable knowledge base (a top-level folder); a ``reference`` scope
    (the bundled platform docs) is read-only.
    """

    id: str
    title: str
    kind: str = "project"  # "project" | "reference"


class EditorData(BaseModel):
    """The ``editor`` archetype's list payload — the browsable document/folder tree."""

    title: str = "Knowledge"
    docs: list[EditorDoc] = Field(default_factory=list)
    can_manage_files: bool = False  # True → the shell shows folder CRUD controls (#216)
    read_only: bool = False  # True → editor is view-only; vault is externally owned (#232)
    # Projects/scopes (#KB-refactor): the knowledge bases the switcher lists, the active
    # one, the noun for its "New …" control, and whether the operator may create another.
    # ``docs`` paths are scope-relative — the shell prepends ``scope`` for read/save/CRUD.
    # An empty ``scope_noun`` means "no switcher" (Notes), keeping the archetype generic.
    scopes: list[EditorScope] = Field(default_factory=list)
    scope: str = ""
    scope_noun: str = ""
    can_create_scope: bool = False
    versioned: bool = True  # True → each save snapshots a version the shell can browse (#ADR-0046)


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


class EditorVersion(BaseModel):
    """One entry in a document's version history (no body — just the metadata)."""

    version_id: str  # opaque to clients = str(row PK)
    created_at: str  # ISO-8601
    title: str  # derived title at that version
    size: int  # character count of the snapshotted content


class EditorVersionList(BaseModel):
    """A document's full version history, newest first."""

    versions: list[EditorVersion] = Field(default_factory=list)


class EditorVersionContent(BaseModel):
    """One past version's full content, returned when the shell opens it."""

    path: str
    version_id: str
    created_at: str
    title: str
    content: str


class MoveBody(BaseModel):
    """Request body for a file/folder move or rename."""

    from_path: str
    to_path: str


class VaultPages:
    """Serves the editor page's data from the operator's Obsidian vault."""

    def __init__(
        self,
        vault_path: Path,
        indexer: KnowledgeIndexer,
        *,
        read_only: bool = False,
        docs_path: Path | None = None,
        versions: VersionStore | None = None,
        tenant: str = "default",
    ) -> None:
        self._vault = vault_path
        self._indexer = indexer
        # Watch mode (#232): the vault is externally owned, so every write is refused and
        # the file-tree CRUD is hidden (the shell honours read_only / can_manage_files).
        self._read_only = read_only
        # The bundled platform docs (read-only), surfaced under the reserved DOCS scope so a
        # service's documentation is readable inside the knowledge base (#KB-refactor, req 3).
        self._docs_path = docs_path
        # Version history (#ADR-0046): each editor save snapshots content here; viewing
        # past versions is allowed even when the vault is read-only. ``None`` (tests) just
        # disables snapshotting — the editor still works.
        self._versions = versions
        self._tenant = tenant

    def _ensure_writable(self) -> None:
        """Reject a mutating operation when the vault is externally owned (#232, ADR-0035)."""
        if self._read_only:
            raise HTTPException(status_code=409, detail=VAULT_READ_ONLY_DETAIL)

    def _is_docs(self, rel: str) -> bool:
        """Whether *rel* targets the reserved, read-only platform-docs scope."""
        return rel == DOCS_SCOPE_ID or rel.startswith(DOCS_SCOPE_ID + "/")

    def _reject_docs_write(self, rel: str) -> None:
        """The platform docs are read-only; refuse any write that targets them."""
        if self._is_docs(rel):
            raise HTTPException(status_code=409, detail="platform docs are read-only")

    def list_scopes(self) -> list[EditorScope]:
        """The knowledge bases (projects) plus the read-only platform-docs reference scope."""
        scopes = [
            EditorScope(id=name, title=name, kind="project") for name in iter_projects(self._vault)
        ]
        if self._docs_path is not None and self._docs_path.exists():
            scopes.append(EditorScope(id=DOCS_SCOPE_ID, title=DOCS_SCOPE_TITLE, kind="reference"))
        return scopes

    def list_docs(self, scope: str = "") -> EditorData:
        """The document/folder tree for one *scope* (knowledge base), depth-first sorted.

        Paths are scope-relative — the shell prepends the active ``scope`` when reading,
        saving, or managing files, so the editor shows a knowledge base's contents without
        the project folder itself appearing as a node. The reserved ``__docs__`` scope
        lists the bundled platform docs read-only (#KB-refactor).
        """

        def _title(node: dict[str, str]) -> str:
            if node["type"] == "file":
                return doc_title(node["path"])
            return node["path"].split("/")[-1]

        scopes = self.list_scopes()
        project_ids = [s.id for s in scopes if s.kind == "project"]
        # Default to the first knowledge base when none is requested.
        active = scope or (project_ids[0] if project_ids else "")

        # The read-only platform-docs scope (req 3): list the bundled docs tree.
        if active == DOCS_SCOPE_ID and self._docs_path is not None:
            nodes = iter_tree_nodes(self._docs_path)
            return EditorData(
                docs=[
                    EditorDoc(id=n["path"], title=_title(n), path=n["path"], type=n["type"])
                    for n in nodes
                ],
                can_manage_files=False,
                read_only=True,
                scopes=scopes,
                scope=DOCS_SCOPE_ID,
                scope_noun=SCOPE_NOUN,
                can_create_scope=not self._read_only,
            )

        # A knowledge-base (project) scope: list just that project's tree, scope-relative.
        nodes = iter_tree_nodes(self._vault, subdir=active) if active in project_ids else []
        # File CRUD is offered only when epicurus may write the vault; a watched external
        # vault is read-only here (Obsidian is the author) so the shell hides the controls.
        return EditorData(
            docs=[
                EditorDoc(id=n["path"], title=_title(n), path=n["path"], type=n["type"])
                for n in nodes
            ],
            can_manage_files=not self._read_only,
            read_only=self._read_only,
            scopes=scopes,
            scope=active,
            scope_noun=SCOPE_NOUN,
            can_create_scope=not self._read_only,
        )

    def create_project(self, name: str) -> EditorScope:
        """Create a new knowledge base — a top-level folder under the vault root.

        409 if it already exists, 400 for an invalid name, 409 when the vault is read-only.
        """
        self._ensure_writable()
        target = safe_project(self._vault, name)
        if target.exists():
            raise HTTPException(status_code=409, detail=f"knowledge base already exists: {name}")
        target.mkdir(parents=True, exist_ok=False)
        log.info("knowledge base created", name=target.name)
        return EditorScope(id=target.name, title=target.name, kind="project")

    def read_doc(self, rel: str) -> EditorDocContent:
        """One document's content. 404 if it does not exist.

        A ``__docs__/…`` path reads from the read-only bundled platform docs; any other
        path is a knowledge-base document under the vault root.
        """
        if self._is_docs(rel):
            if self._docs_path is None:
                raise HTTPException(status_code=404, detail="platform docs are not available")
            target = safe_relative(self._docs_path, rel[len(DOCS_SCOPE_ID) + 1 :])
        else:
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

        409 when the vault is externally owned (watch mode, #232) — Obsidian is the author.
        """
        self._ensure_writable()
        self._reject_docs_write(rel)
        target = safe_relative(self._vault, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        indexed = True
        chunk_count = 0
        try:
            chunk_count = await self._indexer.index_path(rel)
        except Exception as exc:  # the edit is saved; indexing is best-effort here
            log.warning("save succeeded but re-index failed", path=rel, error=str(exc))
            indexed = False
        # Snapshot the saved content for version history (#ADR-0046). The file write above
        # is the source of truth, so record the version even when the re-index failed —
        # and never let a snapshot failure fail the save.
        await self._record_version(rel, content)
        return EditorSaveResult(path=rel, indexed=indexed, chunk_count=chunk_count)

    async def _record_version(self, rel: str, content: str) -> None:
        """Best-effort: append a version-history snapshot for *rel* (never raises)."""
        if self._versions is None:
            return
        try:
            await self._versions.add_version(
                tenant=self._tenant,
                note_path=rel,
                title=doc_title(rel),
                content=content,
            )
        except Exception as exc:  # version history is best-effort; the save already landed
            log.warning("save succeeded but version snapshot failed", path=rel, error=str(exc))

    def create_folder(self, rel: str) -> dict[str, str]:
        """Create a directory at *rel* relative to the vault root.

        409 if it already exists, 400 for any path-safety violation, 409 when the vault is
        externally owned (watch mode, #232).
        """
        self._ensure_writable()
        self._reject_docs_write(rel)
        target = safe_dir_relative(self._vault, rel)
        if target.exists():
            raise HTTPException(status_code=409, detail=f"folder already exists: {rel}")
        target.mkdir(parents=True, exist_ok=False)
        log.info("folder created", path=rel)
        return {"path": rel}

    def delete_doc(self, rel: str) -> None:
        """Delete a ``.md`` file at *rel* relative to the vault root.

        404 if it does not exist, 400 for any path-safety violation, 409 when the vault is
        externally owned (watch mode, #232).
        """
        self._ensure_writable()
        self._reject_docs_write(rel)
        target = safe_relative(self._vault, rel)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"no such document: {rel}")
        target.unlink()
        log.info("document deleted", path=rel)

    def delete_folder(self, rel: str) -> None:
        """Delete an **empty** directory at *rel* relative to the vault root.

        404 if it does not exist, 409 if it is not empty, 400 for any
        path-safety violation, 409 when the vault is externally owned (watch mode, #232).
        """
        self._ensure_writable()
        self._reject_docs_write(rel)
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
        any path-safety violation, 409 when the vault is externally owned (watch mode, #232).
        """
        self._ensure_writable()
        self._reject_docs_write(from_rel)
        self._reject_docs_write(to_rel)
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

    async def list_versions(self, rel: str) -> EditorVersionList:
        """The save-snapshot history for *rel*, newest first (#ADR-0046).

        Viewing history is allowed even when the vault is read-only (watch mode, #232) —
        only *writing* the vault is refused there. The path is still validated through
        :func:`~epicurus_knowledge.refs.safe_relative`.
        """
        safe_relative(self._vault, rel)  # validate the path (400 on traversal / non-md)
        if self._versions is None:
            return EditorVersionList()
        records = await self._versions.list_versions(tenant=self._tenant, note_path=rel)
        return EditorVersionList(
            versions=[
                EditorVersion(
                    version_id=r.version_id,
                    created_at=r.created_at.isoformat(),
                    title=r.title,
                    size=r.size,
                )
                for r in records
            ]
        )

    async def get_version(self, rel: str, version_id: str) -> EditorVersionContent:
        """One past version's full content; 404 when the version does not exist (#ADR-0046).

        Allowed on a read-only vault (viewing, not writing). The path is validated through
        :func:`~epicurus_knowledge.refs.safe_relative`.
        """
        safe_relative(self._vault, rel)  # validate the path (400 on traversal / non-md)
        record = (
            None
            if self._versions is None
            else await self._versions.get_version(
                tenant=self._tenant, note_path=rel, version_id=version_id
            )
        )
        if record is None or record.content is None:
            raise HTTPException(status_code=404, detail=f"no such version: {version_id}")
        return EditorVersionContent(
            path=rel,
            version_id=record.version_id,
            created_at=record.created_at.isoformat(),
            title=record.title,
            content=record.content,
        )


def create_pages_router(pages: VaultPages) -> APIRouter:
    """The HTTP surface the core proxies for the editor page (ADR-0018)."""
    router = APIRouter(tags=["pages"])

    def _require_known_page(page_id: str) -> None:
        if page_id != VAULT_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no such page: {page_id}")

    @router.get("/pages/{page_id}", response_model=EditorData)
    async def get_page(page_id: str, scope: str = Query(default="")) -> EditorData:
        # ``scope`` selects the knowledge base (project) to list; empty = the first one,
        # or the reserved ``__docs__`` for the read-only platform docs (#KB-refactor).
        _require_known_page(page_id)
        return pages.list_docs(scope)

    @router.post("/pages/{page_id}/project", response_model=EditorScope)
    async def post_project(page_id: str, name: str = Query(...)) -> EditorScope:
        # Create a new knowledge base (top-level folder) — the operator's "New knowledge
        # base" control. The agent's equivalent goes through the review queue instead.
        _require_known_page(page_id)
        return pages.create_project(name)

    @router.get("/pages/{page_id}/doc", response_model=EditorDocContent)
    async def get_doc(page_id: str, path: str = Query(...)) -> EditorDocContent:
        _require_known_page(page_id)
        return pages.read_doc(path)

    @router.put("/pages/{page_id}/doc", response_model=EditorSaveResult)
    async def put_doc(page_id: str, body: DocBody, path: str = Query(...)) -> EditorSaveResult:
        _require_known_page(page_id)
        return await pages.write_doc(path, body.content)

    @router.get("/pages/{page_id}/doc/versions", response_model=EditorVersionList)
    async def get_doc_versions(page_id: str, path: str = Query(...)) -> EditorVersionList:
        # Listing history is allowed even for a read-only (watched) vault — viewing only.
        _require_known_page(page_id)
        return await pages.list_versions(path)

    @router.get("/pages/{page_id}/doc/version", response_model=EditorVersionContent)
    async def get_doc_version(
        page_id: str, path: str = Query(...), version: str = Query(...)
    ) -> EditorVersionContent:
        # Fetching a past version is allowed even for a read-only (watched) vault.
        _require_known_page(page_id)
        return await pages.get_version(path, version)

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
