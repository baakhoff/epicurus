"""Tests for the file-tree management surface (#216).

Covers:
- ``safe_dir_relative`` path-safety boundary
- ``iter_tree_nodes`` tree structure and ordering
- ``POST /pages/vault/folder``  — create directory
- ``DELETE /pages/vault/doc``   — delete a .md file
- ``DELETE /pages/vault/folder`` — delete folder (409 when not empty)
- ``POST /pages/vault/move``    — move/rename a file or folder
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from epicurus_knowledge.pages import VaultPages, create_pages_router
from epicurus_knowledge.refs import iter_tree_nodes, safe_dir_relative

# ── Fixtures ──────────────────────────────────────────────────────────────────


class _FakeIndexer:
    """Minimal stand-in — tree ops don't touch the indexer."""

    async def index_path(self, rel: str) -> int:
        return 0


def _vault(tmp_path: Path) -> Path:
    """Populate a small vault: root file, a subdir with a file, a nested subdir."""
    (tmp_path / "alpha.md").write_text("# Alpha\n", encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "beta.md").write_text("# Beta\n", encoding="utf-8")
    nested = projects / "archived"
    nested.mkdir()
    (nested / "gamma.md").write_text("# Gamma\n", encoding="utf-8")
    return tmp_path


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_pages_router(VaultPages(_vault(tmp_path), _FakeIndexer())))
    return TestClient(app, raise_server_exceptions=True)


# ── safe_dir_relative ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    ["../escape", "/etc/passwd", "..\\windows\\evil", "  "],
)
def test_safe_dir_relative_rejects_traversal(tmp_path: Path, bad: str) -> None:
    with pytest.raises(HTTPException) as err:
        safe_dir_relative(tmp_path, bad)
    assert err.value.status_code == 400


def test_safe_dir_relative_accepts_valid_path(tmp_path: Path) -> None:
    target = safe_dir_relative(tmp_path, "projects/archived")
    assert target == (tmp_path.resolve() / "projects" / "archived")


def test_safe_dir_relative_accepts_md_extension(tmp_path: Path) -> None:
    # Unlike safe_relative, safe_dir_relative does NOT require .md — it allows any name.
    target = safe_dir_relative(tmp_path, "notes.txt")
    assert target == (tmp_path.resolve() / "notes.txt")


# ── iter_tree_nodes ───────────────────────────────────────────────────────────


def test_iter_tree_nodes_structure(tmp_path: Path) -> None:
    _vault(tmp_path)
    nodes = iter_tree_nodes(tmp_path)
    paths = [(n["path"], n["type"]) for n in nodes]
    # Dirs appear before their files; depth-first sorted order.
    assert ("projects", "dir") in paths
    assert ("projects/archived", "dir") in paths
    assert ("projects/beta.md", "file") in paths
    assert ("projects/archived/gamma.md", "file") in paths
    assert ("alpha.md", "file") in paths


def test_iter_tree_nodes_dirs_before_files_at_each_level(tmp_path: Path) -> None:
    """Directories must appear before files at the same level."""
    _vault(tmp_path)
    nodes = iter_tree_nodes(tmp_path)
    paths = [n["path"] for n in nodes]
    # "projects" (dir) must come before "alpha.md" (file) at root level.
    assert paths.index("projects") < paths.index("alpha.md")


def test_iter_tree_nodes_empty_root(tmp_path: Path) -> None:
    assert iter_tree_nodes(tmp_path / "absent") == []


def test_iter_tree_nodes_skips_hidden_dirs(tmp_path: Path) -> None:
    hidden = tmp_path / ".obsidian"
    hidden.mkdir()
    (hidden / "secret.md").write_text("x", encoding="utf-8")
    (tmp_path / "visible.md").write_text("y", encoding="utf-8")
    nodes = iter_tree_nodes(tmp_path)
    paths = [n["path"] for n in nodes]
    assert ".obsidian" not in paths
    assert ".obsidian/secret.md" not in paths
    assert "visible.md" in paths


# ── list_docs includes dirs and can_manage_files ──────────────────────────────


def test_list_docs_includes_dirs_and_files(tmp_path: Path) -> None:
    pages = VaultPages(_vault(tmp_path), _FakeIndexer())
    data = pages.list_docs()
    types = {d.path: d.type for d in data.docs}
    assert types["projects"] == "dir"
    assert types["projects/archived"] == "dir"
    assert types["alpha.md"] == "file"
    assert data.can_manage_files is True


# ── POST /pages/vault/folder ──────────────────────────────────────────────────


def test_create_folder_makes_directory(tmp_path: Path) -> None:
    resp = _client(tmp_path).post("/pages/vault/folder", params={"path": "ideas"})
    assert resp.status_code == 200
    assert resp.json() == {"path": "ideas"}
    assert (tmp_path / "ideas").is_dir()


def test_create_folder_409_when_exists(tmp_path: Path) -> None:
    # "projects" is already created by _vault(); calling create on it must 409.
    resp = _client(tmp_path).post("/pages/vault/folder", params={"path": "projects"})
    assert resp.status_code == 409


def test_create_folder_400_on_traversal(tmp_path: Path) -> None:
    resp = _client(tmp_path).post("/pages/vault/folder", params={"path": "../outside"})
    assert resp.status_code == 400


def test_create_folder_nested(tmp_path: Path) -> None:
    resp = _client(tmp_path).post("/pages/vault/folder", params={"path": "deep/nested/dir"})
    assert resp.status_code == 200
    assert (tmp_path / "deep" / "nested" / "dir").is_dir()


# ── DELETE /pages/vault/doc ───────────────────────────────────────────────────


def test_delete_doc_removes_file(tmp_path: Path) -> None:
    resp = _client(tmp_path).delete("/pages/vault/doc", params={"path": "alpha.md"})
    assert resp.status_code == 204
    assert not (tmp_path / "alpha.md").exists()


def test_delete_doc_404_when_absent(tmp_path: Path) -> None:
    resp = _client(tmp_path).delete("/pages/vault/doc", params={"path": "ghost.md"})
    assert resp.status_code == 404


def test_delete_doc_400_on_traversal(tmp_path: Path) -> None:
    resp = _client(tmp_path).delete("/pages/vault/doc", params={"path": "../outside.md"})
    assert resp.status_code == 400


# ── DELETE /pages/vault/folder ────────────────────────────────────────────────


def test_delete_empty_folder(tmp_path: Path) -> None:
    (tmp_path / "empty_dir").mkdir()
    resp = _client(tmp_path).delete("/pages/vault/folder", params={"path": "empty_dir"})
    assert resp.status_code == 204
    assert not (tmp_path / "empty_dir").exists()


def test_delete_folder_409_when_not_empty(tmp_path: Path) -> None:
    # "projects" contains files — must not be deletable.
    resp = _client(tmp_path).delete("/pages/vault/folder", params={"path": "projects"})
    assert resp.status_code == 409


def test_delete_folder_404_when_absent(tmp_path: Path) -> None:
    resp = _client(tmp_path).delete("/pages/vault/folder", params={"path": "ghost"})
    assert resp.status_code == 404


def test_delete_folder_400_on_traversal(tmp_path: Path) -> None:
    resp = _client(tmp_path).delete("/pages/vault/folder", params={"path": "../outside"})
    assert resp.status_code == 400


# ── POST /pages/vault/move ────────────────────────────────────────────────────


def test_move_file_to_new_path(tmp_path: Path) -> None:
    resp = _client(tmp_path).post(
        "/pages/vault/move", json={"from_path": "alpha.md", "to_path": "renamed.md"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"path": "renamed.md"}
    assert not (tmp_path / "alpha.md").exists()
    assert (tmp_path / "renamed.md").is_file()


def test_move_file_into_subdir(tmp_path: Path) -> None:
    resp = _client(tmp_path).post(
        "/pages/vault/move",
        json={"from_path": "alpha.md", "to_path": "projects/alpha.md"},
    )
    assert resp.status_code == 200
    assert (tmp_path / "projects" / "alpha.md").is_file()
    assert not (tmp_path / "alpha.md").exists()


def test_move_folder(tmp_path: Path) -> None:
    resp = _client(tmp_path).post(
        "/pages/vault/move",
        json={"from_path": "projects/archived", "to_path": "projects/old"},
    )
    assert resp.status_code == 200
    assert (tmp_path / "projects" / "old").is_dir()
    assert (tmp_path / "projects" / "old" / "gamma.md").is_file()
    assert not (tmp_path / "projects" / "archived").exists()


def test_move_409_when_destination_exists(tmp_path: Path) -> None:
    resp = _client(tmp_path).post(
        "/pages/vault/move",
        json={"from_path": "alpha.md", "to_path": "projects/beta.md"},
    )
    assert resp.status_code == 409


def test_move_404_when_source_absent(tmp_path: Path) -> None:
    resp = _client(tmp_path).post(
        "/pages/vault/move",
        json={"from_path": "ghost.md", "to_path": "target.md"},
    )
    assert resp.status_code == 404


def test_move_400_on_traversal(tmp_path: Path) -> None:
    resp = _client(tmp_path).post(
        "/pages/vault/move",
        json={"from_path": "../outside.md", "to_path": "target.md"},
    )
    assert resp.status_code == 400


# ── Unknown page_id still returns 404 ────────────────────────────────────────


def test_unknown_page_id_folder_is_404(tmp_path: Path) -> None:
    resp = _client(tmp_path).post("/pages/ghost/folder", params={"path": "x"})
    assert resp.status_code == 404


def test_unknown_page_id_move_is_404(tmp_path: Path) -> None:
    resp = _client(tmp_path).post(
        "/pages/ghost/move", json={"from_path": "a.md", "to_path": "b.md"}
    )
    assert resp.status_code == 404
