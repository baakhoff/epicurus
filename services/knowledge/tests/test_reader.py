"""Tests for the VaultReader read seam (#346, ADR-0070).

The two backends — :class:`DiskVaultReader` and :class:`ApiVaultReader` — must behave
identically over the same tree, so most tests are parametrized over both. The API reader is
backed by a real :class:`~epicurus_core.files.LocalFileStore` (as the core serves it), so a
round-trip through the prefix mapping is exercised end to end.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from epicurus_core.files import FileEntry, LocalFileStore
from epicurus_knowledge.reader import ApiVaultReader, DiskVaultReader, VaultReader

TENANT = "local"
CORE_PREFIX = "knowledge"


class _FilePlatform:
    """A ``PlatformClient`` stand-in whose read ``files_*`` hit a real on-disk store.

    Mirrors how the core serves the file API: ``files_read`` raises ``httpx.HTTPStatusError``
    404 for a missing file, exactly as :class:`~epicurus_core.PlatformClient` does.
    """

    def __init__(self, files_root: Path) -> None:
        self._store = LocalFileStore(files_root)

    async def files_list(self, path: str = "") -> list[FileEntry]:
        return await self._store.list_dir(tenant=TENANT, path=path)

    async def files_stat(self, path: str) -> FileEntry | None:
        return await self._store.stat(tenant=TENANT, path=path)

    async def files_read(self, path: str) -> str:
        try:
            return await self._store.read_text(tenant=TENANT, path=path)
        except FileNotFoundError as exc:
            raise _status_error(404) from exc


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://core/platform/v1/files/read")
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=httpx.Response(code, request=request)
    )


def _w(path: Path, text: str) -> None:
    """Write *text* with ``\\n`` line endings on every platform (no Windows ``\\r\\n``), so the
    byte-preserving readers return deterministic content that matches the Linux runtime."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _seed(vault: Path) -> None:
    """A vault with a project tree, a hidden dir, a reserved (``_``) dir, and non-md files."""
    _w(vault / "a.md", "# A\nbody a")
    _w(vault / "b.md", "# B")
    kb = vault / "kb"
    (kb / "sub").mkdir(parents=True)
    _w(kb / "alpha.md", "# Alpha")
    _w(kb / "sub" / "beta.md", "# Beta")
    _w(kb / "notes.txt", "ignored")  # non-md skipped
    hidden = vault / ".obsidian"
    hidden.mkdir()
    _w(hidden / "conf.md", "cfg")
    (vault / "_reserved").mkdir()  # reserved scope prefix — never a project


# ``Reader`` factories: build the same seeded tree behind each backend.
def _disk(tmp_path: Path) -> VaultReader:
    vault = tmp_path / "vault"
    vault.mkdir()
    _seed(vault)
    return DiskVaultReader(vault)


def _api(tmp_path: Path) -> VaultReader:
    vault = tmp_path / TENANT / CORE_PREFIX
    vault.mkdir(parents=True)
    _seed(vault)
    return ApiVaultReader(_FilePlatform(tmp_path), CORE_PREFIX)  # type: ignore[arg-type]


_READERS: dict[str, Callable[[Path], VaultReader]] = {"disk": _disk, "api": _api}


@pytest.fixture(params=list(_READERS), ids=list(_READERS))
def reader(request: pytest.FixtureRequest, tmp_path: Path) -> VaultReader:
    return _READERS[request.param](tmp_path)


# ── projects: top-level, non-hidden, non-``_`` ───────────────────────────────


async def test_projects_skips_hidden_and_reserved(reader: VaultReader) -> None:
    assert await reader.projects() == ["kb"]


# ── tree: depth-first, dirs before files, hidden dirs skipped ─────────────────


async def test_tree_from_root_skips_hidden_dirs(reader: VaultReader) -> None:
    nodes = await reader.tree()
    paths = {n["path"]: n["type"] for n in nodes}
    assert paths["kb"] == "dir"
    assert paths["kb/sub"] == "dir"
    assert paths["kb/alpha.md"] == "file"
    assert paths["kb/sub/beta.md"] == "file"
    assert paths["a.md"] == "file"
    # Hidden dir and its files never appear; ``_reserved`` (empty) contributes nothing.
    assert ".obsidian" not in paths
    assert ".obsidian/conf.md" not in paths
    # Dirs precede files at the same level; a dir precedes its own descendants.
    order = [n["path"] for n in nodes]
    assert order.index("kb") < order.index("a.md")
    assert order.index("kb") < order.index("kb/alpha.md")


async def test_tree_subdir_is_scope_relative(reader: VaultReader) -> None:
    nodes = await reader.tree("kb")
    paths = {n["path"]: n["type"] for n in nodes}
    assert paths == {"sub": "dir", "sub/beta.md": "file", "alpha.md": "file"}


async def test_tree_absent_subdir_is_empty(reader: VaultReader) -> None:
    assert await reader.tree("ghost") == []


# ── md_files / md_entries: every ``.md`` incl. hidden dirs (indexer reach) ────


async def test_md_files_includes_hidden_dirs_sorted(reader: VaultReader) -> None:
    # Matches the indexer's historical os.walk: hidden dirs are indexed (unlike the UI tree).
    assert await reader.md_files() == [
        ".obsidian/conf.md",
        "a.md",
        "b.md",
        "kb/alpha.md",
        "kb/sub/beta.md",
    ]


async def test_md_entries_carry_mtime(reader: VaultReader) -> None:
    entries = await reader.md_entries()
    by_path = {e.path: e for e in entries}
    assert by_path["a.md"].kind == "file"
    assert by_path["a.md"].mtime > 0  # a real file mtime, for the indexer's change detection


# ── read_text / stat / exists ────────────────────────────────────────────────


async def test_read_text_returns_content(reader: VaultReader) -> None:
    assert await reader.read_text("a.md") == "# A\nbody a"
    assert await reader.read_text("kb/sub/beta.md") == "# Beta"


async def test_read_text_missing_is_none(reader: VaultReader) -> None:
    assert await reader.read_text("kb/ghost.md") is None


async def test_stat_and_exists(reader: VaultReader) -> None:
    assert await reader.is_file("a.md") is True
    assert await reader.is_file("kb") is False  # a dir is not a file
    assert await reader.exists("kb") is True
    assert await reader.exists("ghost.md") is False
    assert await reader.exists() is True  # the vault root itself


async def test_read_text_traversal_is_400(reader: VaultReader) -> None:
    # The reader validates defensively even though callers pre-validate.
    with pytest.raises(HTTPException) as err:
        await reader.read_text("../escape.md")
    assert err.value.status_code == 400


# ── empty / absent vault ─────────────────────────────────────────────────────


async def test_absent_vault_reads_empty_never_raises(tmp_path: Path) -> None:
    # A not-yet-provisioned ``knowledge/`` dir: list/tree/projects are empty, exists() False.
    api = ApiVaultReader(_FilePlatform(tmp_path), CORE_PREFIX)  # type: ignore[arg-type]
    assert await api.list_dir() == []
    assert await api.md_files() == []
    assert await api.projects() == []
    assert await api.exists() is False
    disk = DiskVaultReader(tmp_path / "nope")
    assert await disk.list_dir() == []
    assert await disk.exists() is False


# ── ApiVaultReader error mapping ─────────────────────────────────────────────


class _RaisingPlatform:
    """A platform whose ``files_read`` raises a chosen status, to pin the reader's mapping."""

    def __init__(self, status: int) -> None:
        self._status = status

    async def files_read(self, path: str) -> str:
        raise _status_error(self._status)


@pytest.mark.parametrize("status", [404, 413, 415])
async def test_api_read_text_swallows_missing_and_unreadable(status: int) -> None:
    reader = ApiVaultReader(_RaisingPlatform(status), CORE_PREFIX)  # type: ignore[arg-type]
    assert await reader.read_text("kb/big.md") is None


async def test_api_read_text_reraises_other_errors() -> None:
    # A 500 (or any non-404/413/415) is a real failure — it must propagate so the indexer
    # retries rather than silently treating the file as gone.
    reader = ApiVaultReader(_RaisingPlatform(500), CORE_PREFIX)  # type: ignore[arg-type]
    with pytest.raises(httpx.HTTPStatusError):
        await reader.read_text("kb/x.md")


async def test_api_prefix_mapping_round_trips(tmp_path: Path) -> None:
    """Vault-relative paths map to ``knowledge/<rel>`` and listings strip the prefix back."""
    vault = tmp_path / TENANT / CORE_PREFIX
    (vault / "kb").mkdir(parents=True)
    (vault / "kb" / "note.md").write_text("hi", encoding="utf-8")
    reader = ApiVaultReader(_FilePlatform(tmp_path), CORE_PREFIX)  # type: ignore[arg-type]
    [entry] = await reader.list_dir("kb")
    assert entry.path == "kb/note.md"  # not "knowledge/kb/note.md"
    stat = await reader.stat("kb/note.md")
    assert stat is not None and stat.path == "kb/note.md"


# ── DiskVaultReader: skip-and-log on an unreadable entry (#437) ──────────────
#
# A broken symlink or a permission-denied subdirectory in a watched external vault must not
# fail the whole call — it must be skipped (and logged) while sibling entries still come back.
# CI runs Linux, so a monkeypatch stands in for a real permission-denied/broken-symlink entry.


async def test_list_dir_skips_entry_whose_stat_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _w(vault / "good_a.md", "# A")
    _w(vault / "good_b.md", "# B")
    (vault / "bad").mkdir()  # a dir whose .stat() will be made to fail below

    real_stat = Path.stat

    def _flaky_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "bad":
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _flaky_stat)
    reader = DiskVaultReader(vault)
    entries = await reader.list_dir()

    names = {e.name for e in entries}
    # The unreadable entry is skipped, not raised; its readable siblings still come back.
    assert names == {"good_a.md", "good_b.md"}


async def test_list_dir_skips_entry_whose_is_dir_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _w(vault / "good.md", "# Good")
    (vault / "ghost").mkdir()  # a dir whose .is_dir() will be made to fail below

    real_is_dir = Path.is_dir

    def _flaky_is_dir(self: Path, *args: object, **kwargs: object) -> bool:
        if self.name == "ghost":
            raise PermissionError(13, "Permission denied")
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", _flaky_is_dir)
    reader = DiskVaultReader(vault)
    entries = await reader.list_dir()

    names = {e.name for e in entries}
    assert names == {"good.md"}
    assert "ghost" not in names


async def test_read_text_returns_none_when_read_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file that passes is_file() but raises on the actual read (permission revoked, or
    # vanished, between the check and the read) must degrade to None, not propagate.
    vault = tmp_path / "vault"
    vault.mkdir()
    _w(vault / "locked.md", "secret")
    reader = DiskVaultReader(vault)

    real_read_bytes = Path.read_bytes

    def _flaky_read_bytes(self: Path, *args: object, **kwargs: object) -> bytes:
        if self.name == "locked.md":
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", _flaky_read_bytes)
    assert await reader.read_text("locked.md") is None


async def test_stat_returns_none_when_exists_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A permission-denied target raises out of Path.exists() (it only swallows ENOENT/ENOTDIR,
    # not EACCES) — stat() must report it as absent rather than propagating the OSError.
    vault = tmp_path / "vault"
    vault.mkdir()
    _w(vault / "locked.md", "secret")
    reader = DiskVaultReader(vault)

    real_exists = Path.exists

    def _flaky_exists(self: Path, *args: object, **kwargs: object) -> bool:
        if self.name == "locked.md":
            raise PermissionError(13, "Permission denied")
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", _flaky_exists)
    assert await reader.stat("locked.md") is None
