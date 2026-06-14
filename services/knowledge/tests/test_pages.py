"""Tests for the editor page surface (#130): document list, read, and save.

The indexer is faked — these tests exercise the filesystem contract and the
path-safety boundary, not embeddings or Qdrant.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from epicurus_knowledge.pages import VaultPages, create_pages_router


class _FakeIndexer:
    """Records index_path calls; optionally raises to simulate an embed failure."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self._fail = fail

    async def index_path(self, rel: str) -> int:
        self.calls.append(rel)
        if self._fail:
            raise RuntimeError("embed unavailable")
        return 3


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    sub = tmp_path / "projects"
    sub.mkdir()
    (sub / "beta.md").write_text("# Beta\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")  # non-md is skipped
    return tmp_path


def test_list_docs_returns_sorted_md_only(tmp_path: Path) -> None:
    pages = VaultPages(_vault(tmp_path), _FakeIndexer())
    data = pages.list_docs()
    assert [d.path for d in data.docs] == ["alpha.md", "projects/beta.md"]
    assert data.docs[1].title == "beta"
    assert data.title == "Knowledge"


def test_list_docs_empty_when_no_vault(tmp_path: Path) -> None:
    pages = VaultPages(tmp_path / "absent", _FakeIndexer())
    assert pages.list_docs().docs == []


def test_read_doc_returns_content(tmp_path: Path) -> None:
    pages = VaultPages(_vault(tmp_path), _FakeIndexer())
    doc = pages.read_doc("projects/beta.md")
    assert doc.content == "# Beta\n"
    assert doc.title == "beta"
    assert doc.path == "projects/beta.md"


def test_read_doc_missing_is_404(tmp_path: Path) -> None:
    pages = VaultPages(_vault(tmp_path), _FakeIndexer())
    with pytest.raises(HTTPException) as err:
        pages.read_doc("nope.md")
    assert err.value.status_code == 404


async def test_write_doc_updates_file_and_reindexes(tmp_path: Path) -> None:
    indexer = _FakeIndexer()
    pages = VaultPages(_vault(tmp_path), indexer)
    result = await pages.write_doc("alpha.md", "# Alpha edited\n")
    assert (tmp_path / "alpha.md").read_text(encoding="utf-8") == "# Alpha edited\n"
    assert result.indexed is True
    assert result.chunk_count == 3
    assert indexer.calls == ["alpha.md"]


async def test_write_doc_creates_new_file_with_parents(tmp_path: Path) -> None:
    indexer = _FakeIndexer()
    pages = VaultPages(_vault(tmp_path), indexer)
    result = await pages.write_doc("fresh/idea.md", "seed")
    assert (tmp_path / "fresh" / "idea.md").read_text(encoding="utf-8") == "seed"
    assert result.indexed is True
    assert indexer.calls == ["fresh/idea.md"]


async def test_write_doc_saves_even_when_reindex_fails(tmp_path: Path) -> None:
    # The edit is the source of truth — a failed embed must not lose it.
    indexer = _FakeIndexer(fail=True)
    pages = VaultPages(_vault(tmp_path), indexer)
    result = await pages.write_doc("alpha.md", "kept")
    assert (tmp_path / "alpha.md").read_text(encoding="utf-8") == "kept"
    assert result.indexed is False


@pytest.mark.parametrize(
    "bad",
    ["../escape.md", "/etc/passwd", "..\\windows\\evil.md", "notes.txt", "  "],
)
def test_unsafe_paths_are_rejected(tmp_path: Path, bad: str) -> None:
    pages = VaultPages(_vault(tmp_path), _FakeIndexer())
    with pytest.raises(HTTPException) as err:
        pages.read_doc(bad)
    assert err.value.status_code == 400


async def test_write_rejects_traversal_without_writing(tmp_path: Path) -> None:
    indexer = _FakeIndexer()
    pages = VaultPages(_vault(tmp_path), indexer)
    with pytest.raises(HTTPException):
        await pages.write_doc("../outside.md", "nope")
    assert not (tmp_path.parent / "outside.md").exists()
    assert indexer.calls == []


# ── router (the HTTP surface the core proxies) ────────────────────────────────


def _client(tmp_path: Path, indexer: _FakeIndexer | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(create_pages_router(VaultPages(_vault(tmp_path), indexer or _FakeIndexer())))
    return TestClient(app)


def test_router_lists_documents(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/pages/vault")
    assert resp.status_code == 200
    assert [d["path"] for d in resp.json()["docs"]] == ["alpha.md", "projects/beta.md"]


def test_router_reads_a_document(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/pages/vault/doc", params={"path": "alpha.md"})
    assert resp.status_code == 200
    assert resp.json()["content"] == "# Alpha\n"


def test_router_saves_a_document(tmp_path: Path) -> None:
    indexer = _FakeIndexer()
    resp = _client(tmp_path, indexer).put(
        "/pages/vault/doc", params={"path": "alpha.md"}, json={"content": "new"}
    )
    assert resp.status_code == 200
    assert resp.json()["indexed"] is True
    assert (tmp_path / "alpha.md").read_text(encoding="utf-8") == "new"


def test_router_unknown_page_is_404(tmp_path: Path) -> None:
    assert _client(tmp_path).get("/pages/ghost").status_code == 404


def test_router_traversal_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/pages/vault/doc", params={"path": "../x.md"})
    assert resp.status_code == 400
