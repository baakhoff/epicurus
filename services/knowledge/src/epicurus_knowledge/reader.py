"""The vault **read** seam — how knowledge reads its markdown tree (#346, ADR-0070).

Knowledge's writes already go through the core-owned file API (ADR-0064); this seam moves
its *reads* there too, so the module mounts no ``/data`` volume in the common case and the
read path follows the core's swappable local-FS ↔ S3 backend (constraint #3). Every read
site — the incremental indexer, the editor's ``read_doc`` / ``list_docs``, the attachment
picker, the hover-card resolver, the suggestion-review diff, and the agent read tools —
speaks to a :class:`VaultReader` instead of the filesystem.

Two backends:

* :class:`ApiVaultReader` — reads through :class:`~epicurus_core.PlatformClient` ``files_*``.
  The **default** (normal mode): no ``/data`` mount, backend-agnostic.
* :class:`DiskVaultReader` — reads a local filesystem root. Used for (a) the bundled
  platform docs, always image-mounted at ``/docs`` and outside the file space, and (b) the
  vault in **watch mode** (#232, ADR-0035), where an externally-owned Obsidian vault is
  bind-mounted and ``inotify``-watched. The watcher has no file-API analogue, so watch mode
  keeps its read-only disk mount and reads it directly — byte-for-byte as before.

Every method speaks a **vault-relative** POSIX path (``"kb/note.md"``). The API reader maps
that to the core key ``"<prefix>/kb/note.md"`` (``prefix`` = the vault dir name, ``knowledge``)
and strips the prefix back off listings, so the rest of the module is unaware of the mapping.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from epicurus_core import PlatformClient, get_logger
from epicurus_core.files import FileEntry
from epicurus_knowledge.refs import safe_dir_relative, safe_vault_dir_rel

log = get_logger("knowledge.reader")


class VaultReader(ABC):
    """Reads a tree of markdown documents behind a swappable backend.

    Subclasses implement the three primitives — :meth:`list_dir`, :meth:`read_text`,
    :meth:`stat`; the recursive enumerations (:meth:`projects`, :meth:`tree`,
    :meth:`md_entries`) are shared and built on :meth:`list_dir`.
    """

    @abstractmethod
    async def list_dir(self, rel: str = "") -> list[FileEntry]:
        """The direct children of *rel* (empty = the root).

        Returns ``[]`` when *rel* does not exist or is not a directory — a not-yet-created
        vault reads as empty, never an error. A genuine backend failure (the core is
        unreachable) *raises* instead, so the indexer retries rather than treating the vault
        as emptied and de-indexing everything.
        """

    @abstractmethod
    async def read_text(self, rel: str) -> str | None:
        """The UTF-8 text at *rel*, or ``None`` if it is missing or not readable text.

        ``None`` covers a missing file and — on the API backend — a file the core refuses to
        serve as text (too large, or binary); those are logged and skipped, never fatal.
        """

    @abstractmethod
    async def stat(self, rel: str) -> FileEntry | None:
        """The entry metadata at *rel*, or ``None`` when it does not exist."""

    async def is_file(self, rel: str) -> bool:
        """Whether *rel* exists and is a file."""
        entry = await self.stat(rel)
        return entry is not None and entry.kind == "file"

    async def exists(self, rel: str = "") -> bool:
        """Whether anything exists at *rel* (default: the vault root itself)."""
        return await self.stat(rel) is not None

    # ── recursive enumeration (shared, over list_dir) ────────────────────────────

    async def projects(self) -> list[str]:
        """The knowledge bases — the non-hidden, non-``_`` top-level folders, sorted.

        Mirrors the old ``iter_projects``: dotted dirs are hidden and the ``_`` prefix is
        reserved for virtual scopes (e.g. the platform-docs view), so neither is a project.
        """
        return sorted(
            e.name
            for e in await self.list_dir("")
            if e.kind == "dir" and not e.name.startswith((".", "_"))
        )

    async def tree(self, subdir: str = "") -> list[dict[str, str]]:
        """Depth-first ``{path, type}`` nodes under *subdir*, dirs before files.

        Paths are relative to *subdir* (so a project's contents list without the project
        folder itself appearing). Hidden directories (``.``-prefixed) are skipped; only
        ``.md`` files are emitted. An absent *subdir* yields ``[]``. Mirrors the old
        ``iter_tree_nodes``.
        """
        base = subdir.strip("/")
        nodes: list[dict[str, str]] = []

        async def _walk(rel: str) -> None:
            entries = await self.list_dir(rel)
            subdirs = sorted(
                (e for e in entries if e.kind == "dir" and not e.name.startswith(".")),
                key=lambda e: e.name,
            )
            files = sorted(
                (e for e in entries if e.kind == "file" and e.name.endswith(".md")),
                key=lambda e: e.name,
            )
            # Dirs (each immediately followed by its subtree) before files at this level.
            for sub in subdirs:
                nodes.append({"path": _relative_to(base, sub.path), "type": "dir"})
                await _walk(sub.path)
            for f in files:
                nodes.append({"path": _relative_to(base, f.path), "type": "file"})

        await _walk(base)
        return nodes

    async def md_entries(self) -> list[FileEntry]:
        """Every ``.md`` file under the root (recursive), sorted by path.

        Carries each file's ``mtime`` for the indexer's change detection. Unlike :meth:`tree`
        this does **not** skip hidden directories — it matches the indexer's historical
        ``os.walk`` (and ``iter_md_files``) reach, which indexed every ``.md`` on disk.
        """
        out: list[FileEntry] = []

        async def _walk(rel: str) -> None:
            for e in await self.list_dir(rel):
                if e.kind == "dir":
                    await _walk(e.path)
                elif e.name.endswith(".md"):
                    out.append(e)

        await _walk("")
        out.sort(key=lambda e: e.path)
        return out

    async def md_files(self) -> list[str]:
        """Every ``.md`` file under the root as sorted vault-relative posix paths."""
        return [e.path for e in await self.md_entries()]


def _relative_to(base: str, path: str) -> str:
    """*path* (a full vault-relative posix path) expressed relative to *base*."""
    if not base:
        return path
    return path[len(base) + 1 :] if path.startswith(base + "/") else path


class DiskVaultReader(VaultReader):
    """Reads a local filesystem root — the bundled docs, and the watch-mode vault (#232).

    A read-only view: it never writes. Path safety reuses :func:`refs.safe_dir_relative`
    (the same symlink-confined boundary the write path uses), so a crafted ``rel`` cannot
    escape the root even though the caller has usually already validated it.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _resolve(self, rel: str) -> Path:
        return self._root if not rel else safe_dir_relative(self._root, rel)

    async def list_dir(self, rel: str = "") -> list[FileEntry]:
        base = self._resolve(rel)
        if not base.is_dir():
            return []
        entries: list[FileEntry] = []
        for child in base.iterdir():
            child_rel = f"{rel}/{child.name}" if rel else child.name
            try:
                is_dir = child.is_dir()
                st = child.stat()
            except OSError as exc:
                # A broken symlink or a permission-denied entry in a watched external vault
                # (#437): skip it rather than failing the whole listing — the pre-#434 walk
                # (os.walk, iter_tree_nodes) was tolerant of exactly this, and one unreadable
                # entry must not 500 a tree listing or fail an entire index pass.
                log.warning("skipping unreadable vault entry", path=child_rel, error=str(exc))
                continue
            entries.append(
                FileEntry(
                    path=child_rel,
                    name=child.name,
                    kind="dir" if is_dir else "file",
                    size=0 if is_dir else st.st_size,
                    mtime=0.0 if is_dir else st.st_mtime,
                )
            )
        entries.sort(key=lambda e: (e.kind != "dir", e.name.lower()))
        return entries

    async def read_text(self, rel: str) -> str | None:
        target = self._resolve(rel)
        try:
            if not target.is_file():
                return None
            # Bytes + decode (no universal-newline translation) to match the core file API's
            # byte-preserving read, so both backends return identical content — and identical
            # content hashes — for the same file.
            return target.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            # A permission-denied or vanished-between-check-and-read target (#437):
            # ``Path.is_file()`` only swallows ENOENT/ENOTDIR, not EACCES, so a genuinely
            # unreadable file still raises here. Treat it like "not readable text", per
            # this method's contract, rather than failing the caller (the indexer walk).
            log.warning("skipping unreadable vault file", path=rel, error=str(exc))
            return None

    async def stat(self, rel: str) -> FileEntry | None:
        target = self._resolve(rel)
        try:
            if not target.exists():
                return None
            is_dir = target.is_dir()
            st = target.stat()
        except OSError as exc:
            # Same permission/TOCTOU class as list_dir/read_text (#437): ``Path.exists()``
            # does not swallow EACCES, so a permission-denied target still raises. Report it
            # as absent rather than raising, matching this method's "None when it does not
            # exist" contract.
            log.warning("skipping unreadable vault path", path=rel, error=str(exc))
            return None
        return FileEntry(
            path=rel,
            name=target.name,
            kind="dir" if is_dir else "file",
            size=0 if is_dir else st.st_size,
            mtime=0.0 if is_dir else st.st_mtime,
        )


class ApiVaultReader(VaultReader):
    """Reads the vault through the core file API — the default, backend-agnostic path.

    Vault-relative paths map to the core key ``"<prefix>/<rel>"`` (``prefix`` = the vault dir
    name, ``knowledge``); listings are stripped back to vault-relative so callers stay unaware.
    """

    def __init__(self, platform: PlatformClient, core_prefix: str) -> None:
        self._platform = platform
        self._prefix = core_prefix.strip("/")

    def _core(self, rel: str) -> str:
        rel = rel.strip("/")
        if not rel:
            return self._prefix
        # Validate defensively (the core enforces its own containment too, but never send it a
        # ``..``): a traversal rel raises HTTPException 400, matching the disk reader's guard.
        return f"{self._prefix}/{safe_vault_dir_rel(rel)}"

    def _vault_rel(self, core_path: str) -> str:
        """A core key (``knowledge/kb/x.md``) back to a vault-relative path (``kb/x.md``)."""
        if core_path == self._prefix:
            return ""
        prefix = self._prefix + "/"
        return core_path[len(prefix) :] if core_path.startswith(prefix) else core_path

    def _strip(self, entry: FileEntry) -> FileEntry:
        return entry.model_copy(update={"path": self._vault_rel(entry.path)})

    async def list_dir(self, rel: str = "") -> list[FileEntry]:
        entries = await self._platform.files_list(self._core(rel))
        return [self._strip(e) for e in entries]

    async def read_text(self, rel: str) -> str | None:
        try:
            return await self._platform.files_read(self._core(rel))
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                return None
            if status in (413, 415):
                # Too large / not UTF-8 text — the core caps text reads (413) and refuses
                # binary (415). Skip it (never indexed / never served) rather than fail.
                log.warning("skipping unreadable vault file", path=rel, status=status)
                return None
            raise

    async def stat(self, rel: str) -> FileEntry | None:
        entry = await self._platform.files_stat(self._core(rel))
        return self._strip(entry) if entry is not None else None
