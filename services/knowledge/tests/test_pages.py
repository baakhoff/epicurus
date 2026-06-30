"""Tests for the editor page surface: projects, document list, read, and save.

The knowledge base is organised into **projects** (top-level folders); ``list_docs``
shows one project's contents scope-relative, and the reserved ``__docs__`` scope surfaces
the read-only bundled platform docs (#KB-refactor). The indexer is faked — these tests
exercise the filesystem contract and the path-safety boundary, not embeddings or Qdrant.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from epicurus_core.files import FileEntry, LocalFileStore
from epicurus_knowledge.pages import DOCS_SCOPE_ID, VaultPages, create_pages_router

# The vault is the file-API layout ``<files-root>/<tenant>/knowledge`` (constraint #1): the
# core FileStore is rooted at the files-root, so a vault-relative ``rel`` maps to the core
# path ``knowledge/<rel>``. ``vault_for(tmp_path)`` is the same tree the reads/index walk.
TENANT = "local"
CORE_PREFIX = "knowledge"


def vault_for(files_root: Path) -> Path:
    """The on-disk vault under a fake-core files root: ``<root>/<tenant>/knowledge``."""
    return files_root / TENANT / CORE_PREFIX


class _FilePlatform:
    """A ``PlatformClient`` stand-in whose ``files_*`` delegate to a real on-disk store.

    Migrated writes (write_doc / create_folder / delete_* / move_item / *_project) now go
    through ``platform.files_*``; backing them with a ``LocalFileStore`` rooted at the tmp
    files-root makes them actually hit the tmp tree, so the unchanged reads/index/walk — which
    still read the volume — observe them. ``files_move`` maps the store's
    ``FileExistsError``/``FileNotFoundError``/``ValueError`` to ``httpx.HTTPStatusError``
    409/404/400 so the route's status mapping is exercised, exactly as the real client raises.
    """

    def __init__(self, files_root: Path) -> None:
        self._store = LocalFileStore(files_root)

    @classmethod
    def for_vault(cls, vault: Path) -> _FilePlatform:
        # vault == <files-root>/<tenant>/knowledge → the store roots at <files-root>.
        return cls(vault.parent.parent)

    async def files_write(self, path: str, content: str) -> FileEntry:
        return await self._store.write_text(tenant=TENANT, path=path, content=content)

    async def files_make_dir(self, path: str) -> FileEntry:
        return await self._store.ensure_dir(tenant=TENANT, path=path)

    async def files_stat(self, path: str) -> FileEntry | None:
        return await self._store.stat(tenant=TENANT, path=path)

    async def files_list(self, path: str = "") -> list[FileEntry]:
        return await self._store.list_dir(tenant=TENANT, path=path)

    async def files_delete(self, path: str) -> bool:
        return await self._store.delete(tenant=TENANT, path=path)

    async def files_move(self, src: str, dst: str) -> FileEntry:
        try:
            return await self._store.move(tenant=TENANT, src=src, dst=dst)
        except FileExistsError as exc:
            raise _status_error(409) from exc
        except FileNotFoundError as exc:
            raise _status_error(404) from exc
        except ValueError as exc:
            raise _status_error(400) from exc


def _status_error(code: int) -> httpx.HTTPStatusError:
    """An ``httpx.HTTPStatusError`` carrying *code*, as ``files_move`` raises on conflict."""
    request = httpx.Request("POST", "http://core/platform/v1/files/move")
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=httpx.Response(code, request=request)
    )


class _FakeIndexer:
    """Records index_path / remove_under calls; optionally raises to simulate a failure."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.removed_prefixes: list[str] = []
        self._fail = fail

    async def index_path(self, rel: str) -> int:
        self.calls.append(rel)
        if self._fail:
            raise RuntimeError("embed unavailable")
        return 3

    async def remove_under(self, prefix: str) -> int:
        self.removed_prefixes.append(prefix)
        return 0


def _pages(vault: Path, indexer: object, **kw: object) -> VaultPages:
    """A fake-core-backed ``VaultPages`` over *vault* (writes land on disk via the store)."""
    return VaultPages(
        vault,
        indexer,  # type: ignore[arg-type]
        platform=_FilePlatform.for_vault(vault),  # type: ignore[arg-type]
        core_prefix=CORE_PREFIX,
        **kw,  # type: ignore[arg-type]
    )


def _vault(tmp_path: Path) -> Path:
    """A vault with one knowledge base ``kb`` holding a nested folder and files.

    The vault is ``<tmp>/<tenant>/knowledge`` (the file-API layout): writes go through the
    core to ``<tmp>/<tenant>/knowledge/...`` so they coincide with what reads/index walk.
    """
    vault = vault_for(tmp_path)
    proj = vault / "kb"
    proj.mkdir(parents=True)
    (proj / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    sub = proj / "sub"
    sub.mkdir()
    (sub / "beta.md").write_text("# Beta\n", encoding="utf-8")
    (proj / "notes.txt").write_text("ignored", encoding="utf-8")  # non-md is skipped
    return vault


def _docs(tmp_path: Path) -> Path:
    """A bundled-docs tree (the read-only ``__docs__`` scope)."""
    docs = tmp_path / "docs"
    (docs / "services").mkdir(parents=True)
    (docs / "index.md").write_text("# Platform\n", encoding="utf-8")
    (docs / "services" / "knowledge.md").write_text("# Knowledge service\n", encoding="utf-8")
    return docs


# ── list_docs: a project's tree, scope-relative ──────────────────────────────


def test_list_docs_returns_sorted_md_only(tmp_path: Path) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer())
    data = pages.list_docs()
    # Defaults to the first (only) knowledge base; paths are scope-relative.
    assert data.scope == "kb"
    assert data.scope_noun == "knowledge base"
    assert "kb" in [s.id for s in data.scopes]
    paths = [d.path for d in data.docs]
    assert "sub" in paths
    assert "alpha.md" in paths
    assert "sub/beta.md" in paths
    # Dirs precede same-level files; a dir precedes its own children.
    assert paths.index("sub") < paths.index("alpha.md")
    assert paths.index("sub") < paths.index("sub/beta.md")
    types = {d.path: d.type for d in data.docs}
    assert types["sub"] == "dir"
    assert types["alpha.md"] == "file"
    assert types["sub/beta.md"] == "file"
    beta = next(d for d in data.docs if d.path == "sub/beta.md")
    assert beta.title == "beta"
    assert data.title == "Knowledge"
    assert data.can_manage_files is True
    assert data.can_create_scope is True


def test_list_docs_empty_when_no_vault(tmp_path: Path) -> None:
    pages = _pages(tmp_path / "absent", _FakeIndexer())
    data = pages.list_docs()
    assert data.docs == []
    assert data.scopes == []  # no projects, no docs scope


# ── scopes (projects) ────────────────────────────────────────────────────────


def test_scopes_list_projects_and_default_to_first(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "research").mkdir()
    (vault / "research" / "idea.md").write_text("# Idea\n", encoding="utf-8")
    pages = _pages(vault, _FakeIndexer())
    scopes = {s.id: s.kind for s in pages.list_scopes()}
    assert scopes == {"kb": "project", "research": "project"}
    # Default scope is the first project alphabetically.
    assert pages.list_docs().scope == "kb"


def test_list_docs_scope_selects_project(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "research").mkdir()
    (vault / "research" / "idea.md").write_text("# Idea\n", encoding="utf-8")
    data = _pages(vault, _FakeIndexer()).list_docs("research")
    assert data.scope == "research"
    assert [d.path for d in data.docs] == ["idea.md"]


async def test_create_project_makes_top_level_folder(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    pages = _pages(vault, _FakeIndexer())
    scope = await pages.create_project("Research")
    assert scope.id == "Research"
    assert (vault / "Research").is_dir()
    assert "Research" in [s.id for s in pages.list_scopes()]


async def test_delete_project_removes_dir_and_deindexes(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "research").mkdir()
    (vault / "research" / "idea.md").write_text("# Idea\n", encoding="utf-8")
    indexer = _FakeIndexer()
    pages = _pages(vault, indexer)

    await pages.delete_project("research")
    assert not (vault / "research").exists()
    # The project's documents are de-indexed by its `<name>/` prefix (Qdrant + ledger).
    assert "research/" in indexer.removed_prefixes
    # Other knowledge bases are untouched.
    assert (vault / "kb").is_dir()


async def test_delete_project_unknown_is_404(tmp_path: Path) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer())
    with pytest.raises(HTTPException) as err:
        await pages.delete_project("ghost")
    assert err.value.status_code == 404


async def test_delete_project_invalid_name_is_400(tmp_path: Path) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer())
    with pytest.raises(HTTPException) as err:
        await pages.delete_project("../escape")
    assert err.value.status_code == 400


async def test_delete_project_read_only_is_409(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    indexer = _FakeIndexer()
    pages = _pages(vault, indexer, read_only=True)
    with pytest.raises(HTTPException) as err:
        await pages.delete_project("kb")
    assert err.value.status_code == 409
    assert (vault / "kb").is_dir()  # nothing removed
    assert indexer.removed_prefixes == []


async def test_create_project_409_when_exists(tmp_path: Path) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer())
    with pytest.raises(HTTPException) as err:
        await pages.create_project("kb")
    assert err.value.status_code == 409


@pytest.mark.parametrize("bad", ["a/b", "../x", "__docs__", ".hidden", "  "])
async def test_create_project_400_on_invalid_name(tmp_path: Path, bad: str) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer())
    with pytest.raises(HTTPException) as err:
        await pages.create_project(bad)
    assert err.value.status_code == 400


# ── read / write (project-relative paths) ────────────────────────────────────


def test_read_doc_returns_content(tmp_path: Path) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer())
    doc = pages.read_doc("kb/sub/beta.md")
    assert doc.content == "# Beta\n"
    assert doc.title == "beta"
    assert doc.path == "kb/sub/beta.md"


def test_read_doc_missing_is_404(tmp_path: Path) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer())
    with pytest.raises(HTTPException) as err:
        pages.read_doc("kb/nope.md")
    assert err.value.status_code == 404


async def test_write_doc_updates_file_and_reindexes(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    indexer = _FakeIndexer()
    pages = _pages(vault, indexer)
    result = await pages.write_doc("kb/alpha.md", "# Alpha edited\n")
    assert (vault / "kb" / "alpha.md").read_text(encoding="utf-8") == "# Alpha edited\n"
    assert result.indexed is True
    assert result.chunk_count == 3
    assert indexer.calls == ["kb/alpha.md"]


async def test_write_doc_creates_new_file_with_parents(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    indexer = _FakeIndexer()
    pages = _pages(vault, indexer)
    result = await pages.write_doc("kb/fresh/idea.md", "seed")
    assert (vault / "kb" / "fresh" / "idea.md").read_text(encoding="utf-8") == "seed"
    assert result.indexed is True
    assert indexer.calls == ["kb/fresh/idea.md"]


async def test_write_doc_saves_even_when_reindex_fails(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    indexer = _FakeIndexer(fail=True)
    pages = _pages(vault, indexer)
    result = await pages.write_doc("kb/alpha.md", "kept")
    assert (vault / "kb" / "alpha.md").read_text(encoding="utf-8") == "kept"
    assert result.indexed is False


async def test_write_doc_targets_the_knowledge_core_path(tmp_path: Path) -> None:
    """A vault-relative ``rel`` must be written to the core path ``knowledge/<rel>`` (ADR-0064).

    Records what reaches ``platform.files_write`` to pin the prefix mapping: the core FileStore
    root is the tenant dir, so the vault (``.../knowledge``) prefixes every key with ``knowledge``.
    """
    vault = _vault(tmp_path)
    seen: list[str] = []
    platform = _FilePlatform.for_vault(vault)
    inner = platform.files_write

    async def _spy(path: str, content: str) -> FileEntry:
        seen.append(path)
        return await inner(path, content)

    platform.files_write = _spy  # type: ignore[method-assign]
    pages = VaultPages(
        vault,
        _FakeIndexer(),  # type: ignore[arg-type]
        platform=platform,  # type: ignore[arg-type]
        core_prefix=CORE_PREFIX,
    )
    await pages.write_doc("kb/sub/beta.md", "x")
    assert seen == ["knowledge/kb/sub/beta.md"]


@pytest.mark.parametrize(
    "bad",
    ["../escape.md", "/etc/passwd", "..\\windows\\evil.md", "kb/notes.txt", "  "],
)
def test_unsafe_paths_are_rejected(tmp_path: Path, bad: str) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer())
    with pytest.raises(HTTPException) as err:
        pages.read_doc(bad)
    assert err.value.status_code == 400


async def test_write_rejects_traversal_without_writing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    indexer = _FakeIndexer()
    pages = _pages(vault, indexer)
    with pytest.raises(HTTPException):
        await pages.write_doc("../outside.md", "nope")
    assert not (vault.parent / "outside.md").exists()
    assert indexer.calls == []


# ── docs scope (read-only platform docs, req 3) ──────────────────────────────


def test_docs_scope_listed_and_read_only(tmp_path: Path) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer(), docs_path=_docs(tmp_path))
    assert (DOCS_SCOPE_ID, "reference") in [(s.id, s.kind) for s in pages.list_scopes()]
    data = pages.list_docs(DOCS_SCOPE_ID)
    assert data.read_only is True
    assert data.can_manage_files is False
    paths = [d.path for d in data.docs]
    assert "index.md" in paths
    assert "services/knowledge.md" in paths


def test_docs_scope_read_doc(tmp_path: Path) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer(), docs_path=_docs(tmp_path))
    doc = pages.read_doc(f"{DOCS_SCOPE_ID}/services/knowledge.md")
    assert doc.content == "# Knowledge service\n"


async def test_docs_scope_writes_are_409(tmp_path: Path) -> None:
    pages = _pages(_vault(tmp_path), _FakeIndexer(), docs_path=_docs(tmp_path))
    with pytest.raises(HTTPException) as err:
        await pages.write_doc(f"{DOCS_SCOPE_ID}/index.md", "nope")
    assert err.value.status_code == 409
    with pytest.raises(HTTPException) as err2:
        await pages.create_folder(f"{DOCS_SCOPE_ID}/newdir")
    assert err2.value.status_code == 409


# ── router (the HTTP surface the core proxies) ───────────────────────────────


def _client(tmp_path: Path, indexer: _FakeIndexer | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(create_pages_router(_pages(_vault(tmp_path), indexer or _FakeIndexer())))
    return TestClient(app)


def test_router_lists_documents(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/pages/vault")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "kb"
    paths = [d["path"] for d in body["docs"]]
    assert "sub" in paths
    assert "alpha.md" in paths
    assert "sub/beta.md" in paths
    by_path = {d["path"]: d for d in body["docs"]}
    assert by_path["sub"]["type"] == "dir"
    assert by_path["alpha.md"]["type"] == "file"


def test_router_scope_query_selects_project(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/pages/vault", params={"scope": "kb"})
    assert resp.status_code == 200
    assert resp.json()["scope"] == "kb"


def test_router_creates_project(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    app = FastAPI()
    app.include_router(create_pages_router(_pages(vault, _FakeIndexer())))
    resp = TestClient(app).post("/pages/vault/project", params={"name": "research"})
    assert resp.status_code == 200
    assert resp.json()["id"] == "research"
    assert (vault / "research").is_dir()


def test_router_deletes_project(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "research").mkdir()
    (vault / "research" / "idea.md").write_text("# Idea\n", encoding="utf-8")
    indexer = _FakeIndexer()
    app = FastAPI()
    app.include_router(create_pages_router(_pages(vault, indexer)))
    resp = TestClient(app).delete("/pages/vault/project", params={"name": "research"})
    assert resp.status_code == 204
    assert not (vault / "research").exists()
    assert "research/" in indexer.removed_prefixes


def test_router_reads_a_document(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/pages/vault/doc", params={"path": "kb/alpha.md"})
    assert resp.status_code == 200
    assert resp.json()["content"] == "# Alpha\n"


def test_router_saves_a_document(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    app = FastAPI()
    indexer = _FakeIndexer()
    app.include_router(create_pages_router(_pages(vault, indexer)))
    resp = TestClient(app).put(
        "/pages/vault/doc", params={"path": "kb/alpha.md"}, json={"content": "new"}
    )
    assert resp.status_code == 200
    assert resp.json()["indexed"] is True
    assert (vault / "kb" / "alpha.md").read_text(encoding="utf-8") == "new"


def test_router_unknown_page_is_404(tmp_path: Path) -> None:
    assert _client(tmp_path).get("/pages/ghost").status_code == 404


def test_router_traversal_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/pages/vault/doc", params={"path": "../x.md"})
    assert resp.status_code == 400


# ── read-only (watched external vault, #232) ─────────────────────────────────


def test_read_only_marks_view_only_and_hides_file_crud(tmp_path: Path) -> None:
    data = _pages(_vault(tmp_path), _FakeIndexer(), read_only=True).list_docs()
    assert data.read_only is True
    assert data.can_manage_files is False
    # The tree is still listed — read-only means view-only, not invisible.
    assert any(d.path == "alpha.md" for d in data.docs)


async def test_read_only_write_doc_is_409_and_does_not_write(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    indexer = _FakeIndexer()
    pages = _pages(vault, indexer, read_only=True)
    with pytest.raises(HTTPException) as err:
        await pages.write_doc("kb/alpha.md", "should not land")
    assert err.value.status_code == 409
    assert (vault / "kb" / "alpha.md").read_text(encoding="utf-8") == "# Alpha\n"
    assert indexer.calls == []


async def test_read_only_rejects_folder_and_move_operations(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    pages = _pages(vault, _FakeIndexer(), read_only=True)
    # The folder-CRUD / move / project ops are async now (they write via the core file API),
    # so each must be awaited; the read-only guard still rejects with 409 before any I/O.
    for call in (
        pages.create_folder("kb/newdir"),
        pages.delete_doc("kb/alpha.md"),
        pages.delete_folder("kb/sub"),
        pages.move_item("kb/alpha.md", "kb/renamed.md"),
        pages.create_project("research"),
    ):
        with pytest.raises(HTTPException) as err:
            await call
        assert err.value.status_code == 409
    assert (vault / "kb" / "alpha.md").is_file()
    assert (vault / "kb" / "sub" / "beta.md").is_file()
    assert not (vault / "kb" / "newdir").exists()


def test_router_save_is_409_when_read_only(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(
        create_pages_router(_pages(_vault(tmp_path), _FakeIndexer(), read_only=True))
    )
    client = TestClient(app)
    listing = client.get("/pages/vault")
    assert listing.json()["read_only"] is True
    assert listing.json()["can_manage_files"] is False
    resp = client.put("/pages/vault/doc", params={"path": "kb/alpha.md"}, json={"content": "x"})
    assert resp.status_code == 409
