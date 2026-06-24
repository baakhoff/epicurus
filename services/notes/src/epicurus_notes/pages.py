"""The ``editor`` page the notes module contributes (ADR-0018 / ADR-0022, #134).

The web shell renders the Obsidian-like editor from the bounded core vocabulary;
this module supplies **data only** over the three endpoints the core proxies:

* ``GET /pages/{page_id}`` — the document list (``EditorData``).
* ``GET /pages/{page_id}/doc?path=<slug>`` — one note's content (``EditorDocContent``).
* ``PUT /pages/{page_id}/doc?path=<slug>`` — save a note, creating it when the slug is
  new, then re-index it into the tenant's ``notes`` collection.

Two things differ from the knowledge vault (which reuses this same contract):

* **No filesystem.** A note is addressed by a tenant-unique ``slug`` that is a
  Postgres key, not a path — there is no traversal surface, only slug validation.
  A ``/`` in a slug groups notes into folders; empty folders are persisted in
  :class:`~epicurus_notes.db.NoteFolderStore` so they survive a reload.
* **File management.** ``EditorData.can_manage_files`` is ``True``, so the shared editor
  shows the same folder/document controls as the knowledge base (#KB-refactor) — New
  document, New folder, new-file-in-folder, rename, delete — but **without projects**
  (notes are a single flat space). The agent never manages folders: this surface is the
  operator's, and notes stay private (see :mod:`epicurus_notes.service`).

``title`` is derived from the body (its first heading / line) so the ``{content}``-only
save contract (ADR-0022) needs no title field.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from epicurus_core import get_logger
from epicurus_notes.db import NoteFolderStore, NotesStore
from epicurus_notes.indexer import NotesIndexer
from epicurus_notes.mirror import NotesMirror

log = get_logger("notes.pages")

# The single editor page id this module declares (see service.py manifest `pages`).
NOTES_PAGE_ID = "notes"

_MAX_SLUG = 512
_MAX_TITLE = 200


class EditorDoc(BaseModel):
    """One entry in the editor's document/folder tree."""

    id: str
    title: str
    path: str
    type: str = "file"  # "file" | "dir"


class EditorData(BaseModel):
    """The ``editor`` archetype's list payload — the browsable document/folder tree.

    ``can_manage_files`` opts this page into the shared editor's file-management controls
    (#216): New document, New folder, new-file-in-folder, rename, delete. Notes use this
    (not the simpler ``can_create``) so they get folders — but with no project switcher
    (``scope_noun`` stays empty), keeping notes a single flat space.
    """

    title: str = "Notes"
    docs: list[EditorDoc] = Field(default_factory=list)
    can_manage_files: bool = True


class EditorDocContent(BaseModel):
    """One note's full content, returned when the editor opens it."""

    path: str
    title: str
    content: str


class DocBody(BaseModel):
    """The save request body: the note's full new content."""

    content: str


class EditorSaveResult(BaseModel):
    """The outcome of a save: the slug, and whether the re-index succeeded."""

    path: str
    indexed: bool
    chunk_count: int = 0


class MoveBody(BaseModel):
    """Request body for renaming/moving a note (re-keying its slug)."""

    from_path: str
    to_path: str


def _clean_slug(slug: str) -> str:
    """Validate the editor ``path`` as a note slug — the write trust boundary.

    A slug is a Postgres key (no filesystem), so there is no traversal to defend;
    we only reject the empty, the over-long, and anything carrying control
    characters or surrounding whitespace (which would desync the shell's path from
    the stored key). The slug is stored verbatim.
    """
    if slug != slug.strip() or not slug:
        raise HTTPException(status_code=400, detail="slug is required")
    if len(slug) > _MAX_SLUG:
        raise HTTPException(status_code=400, detail="slug is too long")
    if any(ord(ch) < 0x20 for ch in slug):
        raise HTTPException(status_code=400, detail="slug has invalid characters")
    return slug


def _clean_dir(path: str) -> str:
    """Validate + normalise a folder path (the tree's directory key).

    Like a slug a folder path is a Postgres key, but it is also a ``/``-joined tree path,
    so we additionally reject empty segments and ``.``/``..`` — both so the tree stays
    well-formed and as defence-in-depth for the ``.md`` mirror that writes under it.
    """
    p = path.strip().strip("/")
    if not p:
        raise HTTPException(status_code=400, detail="folder path is required")
    if len(p) > _MAX_SLUG:
        raise HTTPException(status_code=400, detail="folder path is too long")
    segments = p.split("/")
    for seg in segments:
        if not seg or seg in {".", ".."} or any(ord(ch) < 0x20 for ch in seg):
            raise HTTPException(status_code=400, detail="folder path has invalid segments")
    return "/".join(segments)


def _ancestor_dirs(path: str) -> list[str]:
    """Every directory prefix of *path* (e.g. ``a/b/c`` → ``[a, a/b]`` for a slug,
    ``[a, a/b, a/b/c]`` when *path* is itself a folder is handled by the caller)."""
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def derive_title(content: str) -> str:
    """A note's display title: its first heading or non-empty line, else 'Untitled'."""
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:_MAX_TITLE]
    return "Untitled"


class NotesPages:
    """Serves the editor page's data from the notes store + vector index."""

    def __init__(
        self,
        store: NotesStore,
        indexer: NotesIndexer,
        *,
        tenant: str,
        on_saved: Callable[[str], Awaitable[None]] | None = None,
        mirror: NotesMirror | None = None,
        folders: NoteFolderStore | None = None,
    ) -> None:
        self._store = store
        self._indexer = indexer
        self._tenant = tenant
        self._on_saved = on_saved
        # Writes a read-only .md copy into the shared file space so notes show in Files
        # (#KB-refactor, req 7). None disables it (tests / no mount).
        self._mirror = mirror
        # Persists empty/explicit folders (#KB-refactor). None ⇒ folders are derived only
        # from note slugs (no standalone empty folders); folder CRUD is then unavailable.
        self._folders = folders

    def _require_folders(self) -> NoteFolderStore:
        if self._folders is None:  # pragma: no cover - always wired in app.py
            raise HTTPException(status_code=409, detail="folder management is not configured")
        return self._folders

    async def list_docs(self) -> EditorData:
        """The note tree: folder nodes then file nodes, for the shared editor (#KB-refactor).

        Directories come from the union of every explicitly-created folder and every
        ``/``-bearing note slug's prefixes; they are emitted **before** any file and sorted
        so a parent always precedes its children (the shell's tree builder relies on this).
        Files are the notes themselves, titled from their bodies.
        """
        summaries = await self._store.list_summaries(tenant=self._tenant)
        folder_rows = await self._folders.list(tenant=self._tenant) if self._folders else []

        dirs: set[str] = set()
        for folder in folder_rows:
            dirs.add(folder)
            dirs.update(_ancestor_dirs(folder))
        for summary in summaries:
            dirs.update(_ancestor_dirs(summary.slug))

        docs = [EditorDoc(id=d, title=d.split("/")[-1], path=d, type="dir") for d in sorted(dirs)]
        docs += [EditorDoc(id=s.slug, title=s.title, path=s.slug, type="file") for s in summaries]
        return EditorData(docs=docs)

    async def create_folder(self, path: str) -> dict[str, str]:
        """Create an empty folder at *path*. 409 if it already exists, 400 if invalid."""
        folders = self._require_folders()
        clean = _clean_dir(path)
        created = await folders.add(tenant=self._tenant, path=clean)
        if not created:
            raise HTTPException(status_code=409, detail=f"folder already exists: {clean}")
        log.info("note folder created", path=clean)
        return {"path": clean}

    async def delete_folder(self, path: str) -> None:
        """Delete an **empty** folder at *path*.

        Empty means no note slug and no child folder lives under ``<path>/``. 404 if the
        folder does not exist (it was never created and nothing implies it), 409 if not empty.
        """
        folders = self._require_folders()
        clean = _clean_dir(path)
        prefix = clean + "/"
        summaries = await self._store.list_summaries(tenant=self._tenant)
        if any(s.slug.startswith(prefix) for s in summaries):
            raise HTTPException(status_code=409, detail=f"folder is not empty: {clean}")
        rows = await folders.list(tenant=self._tenant)
        if any(r.startswith(prefix) for r in rows):
            raise HTTPException(status_code=409, detail=f"folder is not empty: {clean}")
        deleted = await folders.delete(tenant=self._tenant, path=clean)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"no such folder: {clean}")
        log.info("note folder deleted", path=clean)

    async def move_item(self, from_path: str, to_path: str) -> dict[str, str]:
        """Rename/move a note by re-keying its slug (the editor renames files only).

        404 if the source note does not exist, 409 if the destination slug is taken. The
        note's vectors and ``.md`` mirror follow the new slug; both are best-effort, the
        Postgres re-key is the source of truth.
        """
        src = _clean_slug(from_path)
        dst = _clean_slug(to_path)
        if src == dst:
            return {"path": dst}
        note = await self._store.get(tenant=self._tenant, slug=src)
        if note is None:
            raise HTTPException(status_code=404, detail=f"no such note: {src}")
        if await self._store.get(tenant=self._tenant, slug=dst) is not None:
            raise HTTPException(status_code=409, detail=f"destination already exists: {dst}")
        await self._store.upsert(
            tenant=self._tenant, slug=dst, title=note.title, content=note.content
        )
        await self._store.delete(tenant=self._tenant, slug=src)
        try:
            await self._indexer.index_note(dst, note.content)
            await self._indexer.delete_note(src)
        except Exception as exc:  # the row is re-keyed; vectors are derived
            log.warning("note moved but re-index failed", from_=src, to=dst, error=str(exc))
        if self._mirror is not None:
            await self._mirror.write(dst, note.content)
            await self._mirror.delete(src)
        log.info("note moved", from_=src, to=dst)
        return {"path": dst}

    async def read_doc(self, slug: str) -> EditorDocContent:
        """One note's content. 404 if it does not exist."""
        slug = _clean_slug(slug)
        note = await self._store.get(tenant=self._tenant, slug=slug)
        if note is None:
            raise HTTPException(status_code=404, detail=f"no such note: {slug}")
        return EditorDocContent(path=note.slug, title=note.title, content=note.content)

    async def write_doc(self, slug: str, content: str) -> EditorSaveResult:
        """Create or update a note, then re-index it.

        Postgres is the source of truth and is written first; if the embed
        round-trip fails (e.g. the core is paused) the save still succeeds with
        ``indexed=False`` so an edit is never lost — the next save retries the index.
        """
        slug = _clean_slug(slug)
        title = derive_title(content)
        await self._store.upsert(tenant=self._tenant, slug=slug, title=title, content=content)
        # Mirror to the shared file space so the note shows in Files (#KB-refactor, req 7).
        # Best-effort and never raises; runs before indexing so the file reflects the saved
        # body even if the embed round-trip fails.
        if self._mirror is not None:
            await self._mirror.write(slug, content)
        try:
            chunk_count = await self._indexer.index_note(slug, content)
        except Exception as exc:  # the note is saved; indexing is best-effort
            log.warning("note saved but re-index failed", slug=slug, error=str(exc))
            return EditorSaveResult(path=slug, indexed=False)
        if self._on_saved is not None:
            try:
                await self._on_saved(slug)
            except Exception as exc:  # observability only — never fail a save on it
                log.warning("notes.saved publish failed", slug=slug, error=str(exc))
        return EditorSaveResult(path=slug, indexed=True, chunk_count=chunk_count)

    async def delete_doc(self, slug: str) -> None:
        """Delete a note — its Postgres row, its vectors, and its ``.md`` mirror.

        404 if it does not exist. Used by an approved ``delete`` suggestion (#KB-refactor);
        de-index and mirror removal are best-effort, the row delete is the source of truth.
        """
        slug = _clean_slug(slug)
        deleted = await self._store.delete(tenant=self._tenant, slug=slug)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"no such note: {slug}")
        try:
            await self._indexer.delete_note(slug)
        except Exception as exc:  # the note is gone from the store; vectors are derived
            log.warning("note deleted but de-index failed", slug=slug, error=str(exc))
        if self._mirror is not None:
            await self._mirror.delete(slug)


def create_pages_router(pages: NotesPages) -> APIRouter:
    """The HTTP surface the core proxies for the editor page (ADR-0018)."""
    router = APIRouter(tags=["pages"])

    def _require_known_page(page_id: str) -> None:
        if page_id != NOTES_PAGE_ID:
            raise HTTPException(status_code=404, detail=f"no such page: {page_id}")

    @router.get("/pages/{page_id}", response_model=EditorData)
    async def get_page(page_id: str) -> EditorData:
        _require_known_page(page_id)
        return await pages.list_docs()

    @router.get("/pages/{page_id}/doc", response_model=EditorDocContent)
    async def get_doc(page_id: str, path: str = Query(...)) -> EditorDocContent:
        _require_known_page(page_id)
        return await pages.read_doc(path)

    @router.put("/pages/{page_id}/doc", response_model=EditorSaveResult)
    async def put_doc(page_id: str, body: DocBody, path: str = Query(...)) -> EditorSaveResult:
        _require_known_page(page_id)
        return await pages.write_doc(path, body.content)

    @router.post("/pages/{page_id}/folder")
    async def post_folder(page_id: str, path: str = Query(...)) -> dict[str, str]:
        _require_known_page(page_id)
        return await pages.create_folder(path)

    @router.delete("/pages/{page_id}/doc", status_code=204)
    async def delete_doc(page_id: str, path: str = Query(...)) -> None:
        _require_known_page(page_id)
        await pages.delete_doc(path)

    @router.delete("/pages/{page_id}/folder", status_code=204)
    async def delete_folder(page_id: str, path: str = Query(...)) -> None:
        _require_known_page(page_id)
        await pages.delete_folder(path)

    @router.post("/pages/{page_id}/move")
    async def post_move(page_id: str, body: MoveBody) -> dict[str, str]:
        _require_known_page(page_id)
        return await pages.move_item(body.from_path, body.to_path)

    return router
