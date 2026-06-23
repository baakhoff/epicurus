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
* **Authoring.** ``EditorData.can_create`` is ``True``, so the shared editor shows a
  "New note" affordance; saving to a new slug *creates* the note (knowledge leaves
  this ``False`` — its notes are authored externally in Obsidian).

``title`` is derived from the body (its first heading / line) so the ``{content}``-only
save contract (ADR-0022) needs no title field.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from epicurus_core import get_logger
from epicurus_notes.db import NotesStore
from epicurus_notes.indexer import NotesIndexer

log = get_logger("notes.pages")

# The single editor page id this module declares (see service.py manifest `pages`).
NOTES_PAGE_ID = "notes"

_MAX_SLUG = 512
_MAX_TITLE = 200


class EditorDoc(BaseModel):
    """One entry in the editor's document list."""

    id: str
    title: str
    path: str


class EditorData(BaseModel):
    """The ``editor`` archetype's list payload — the browsable set of documents.

    ``can_create`` opts this page into the shared editor's authoring affordance
    (ADR-0026): the shell shows a "New note" control that saves to a new slug.
    ``versioned`` opts it into version history (ADR-0045): every save snapshots the
    body, and the shell offers a browse/restore-past-versions affordance.
    """

    title: str = "Notes"
    docs: list[EditorDoc] = Field(default_factory=list)
    can_create: bool = True
    versioned: bool = True


class EditorDocContent(BaseModel):
    """One note's full content, returned when the editor opens it."""

    path: str
    title: str
    content: str


class EditorVersion(BaseModel):
    """One entry in a note's version list (ADR-0045)."""

    version_id: str
    created_at: str
    title: str
    size: int


class EditorVersionList(BaseModel):
    """A note's past versions, newest first."""

    versions: list[EditorVersion] = Field(default_factory=list)


class EditorVersionContent(BaseModel):
    """One past version's full content, returned when the editor opens it."""

    path: str
    version_id: str
    created_at: str
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
    ) -> None:
        self._store = store
        self._indexer = indexer
        self._tenant = tenant
        self._on_saved = on_saved

    async def list_docs(self) -> EditorData:
        """Every note for the tenant, newest first (no bodies)."""
        summaries = await self._store.list_summaries(tenant=self._tenant)
        docs = [EditorDoc(id=s.slug, title=s.title, path=s.slug) for s in summaries]
        return EditorData(docs=docs)

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
        # Snapshot the saved body for version history (ADR-0045). The save has
        # already committed, so a failure here must never fail the save — it only
        # loses one history entry.
        try:
            await self._store.add_version(
                tenant=self._tenant, slug=slug, title=title, content=content
            )
        except Exception as exc:  # the note is saved; the snapshot is best-effort
            log.warning("note saved but version snapshot failed", slug=slug, error=str(exc))
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

    async def list_versions(self, slug: str) -> EditorVersionList:
        """A note's past versions, newest first (no bodies) — ADR-0045."""
        slug = _clean_slug(slug)
        summaries = await self._store.list_versions(tenant=self._tenant, slug=slug)
        return EditorVersionList(
            versions=[
                EditorVersion(
                    version_id=s.version_id,
                    created_at=s.created_at.isoformat(),
                    title=s.title,
                    size=s.size,
                )
                for s in summaries
            ]
        )

    async def get_version(self, slug: str, version_id: str) -> EditorVersionContent:
        """One past version's full content. 404 if it is not this note's version."""
        slug = _clean_slug(slug)
        version = await self._store.get_version(
            tenant=self._tenant, slug=slug, version_id=version_id
        )
        if version is None:
            raise HTTPException(status_code=404, detail=f"no such version: {version_id}")
        return EditorVersionContent(
            path=version.slug,
            version_id=version.version_id,
            created_at=version.created_at.isoformat(),
            title=version.title,
            content=version.content,
        )


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

    @router.get("/pages/{page_id}/doc/versions", response_model=EditorVersionList)
    async def get_versions(page_id: str, path: str = Query(...)) -> EditorVersionList:
        _require_known_page(page_id)
        return await pages.list_versions(path)

    @router.get("/pages/{page_id}/doc/version", response_model=EditorVersionContent)
    async def get_version(
        page_id: str, path: str = Query(...), version: str = Query(...)
    ) -> EditorVersionContent:
        _require_known_page(page_id)
        return await pages.get_version(path, version)

    return router
