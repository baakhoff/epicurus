"""Stable references and path-safety for knowledge documents.

A knowledge document is named to the rest of the platform by an opaque, URL-safe
``ref_id`` — used both as a chat **entity reference** (the hover-card resolver,
#143/ADR-0019) and as a chat **attachment** (#137). The id encodes the document's
*source* (the operator's vault, or the bundled platform docs) and its source-relative
path, so a single ``kind="knowledge"`` reference round-trips back to exactly one file —
unambiguous even when a vault happens to contain a top-level ``docs/`` folder.

The encoding is base64url of ``"<source>:<path>"``. base64url is deliberate: the core
proxies resolves at ``GET /resolve/{kind}/{ref_id}`` and attachments at
``GET /attachments/{ref_id}``, where ``ref_id`` is a single path segment that matches
``[^/]+``. A raw vault path contains ``/`` and would neither match that route nor survive
an intermediary's ``%2F`` handling; an opaque, slash-free id sidesteps both.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

__all__ = [
    "KNOWLEDGE_KIND",
    "SOURCE_DOC",
    "SOURCE_NOTE",
    "decode_ref",
    "doc_title",
    "encode_ref",
    "iter_md_files",
    "iter_tree_nodes",
    "safe_dir_relative",
    "safe_relative",
]

# Every knowledge entity — a chat attachment (#137) or a cited reference (#143) —
# carries this single ``kind`` (ADR-0019); the source lives inside the opaque ref_id.
KNOWLEDGE_KIND = "knowledge"

# A reference points at one of two markdown sources the module indexes.
SOURCE_NOTE = "note"  # the operator's Obsidian vault (editable in the Knowledge page)
SOURCE_DOC = "doc"  # the bundled platform docs (read-only self-documentation)
_SOURCES = frozenset({SOURCE_NOTE, SOURCE_DOC})


def encode_ref(source: str, path: str) -> str:
    """Encode a ``(source, path)`` pair into an opaque, URL-safe ``ref_id``."""
    raw = f"{source}:{path}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_ref(ref_id: str) -> tuple[str, str]:
    """Decode a ``ref_id`` back to ``(source, path)``; 404 on anything malformed.

    A bad id is *not found*, not a server error — it reaches us only through a
    user- or agent-supplied reference, so it is never trusted.
    """
    padding = "=" * (-len(ref_id) % 4)
    try:
        decoded = base64.urlsafe_b64decode(ref_id + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=404, detail="unknown reference") from exc
    source, sep, path = decoded.partition(":")
    if not sep or source not in _SOURCES or not path:
        raise HTTPException(status_code=404, detail="unknown reference")
    return source, path


def doc_title(rel: str) -> str:
    """Display title for a document — its file name without the ``.md`` suffix."""
    return PurePosixPath(rel).stem


def safe_relative(root: Path, rel: str) -> Path:
    """Resolve *rel* against *root*, refusing anything that escapes it.

    The shared trust boundary for every surface that reads a user- or agent-named
    document (the editor, the hover-card resolver, the attachment resolve). Rejects
    absolute paths, ``..`` traversal, symlink escapes, and any non-``.md`` target; the
    resolved file must live under *root*.
    """
    cleaned = rel.strip().replace("\\", "/")
    if not cleaned:
        raise HTTPException(status_code=400, detail="path is required")
    candidate = PurePosixPath(cleaned)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="path escapes the root")
    root_resolved = root.resolve()
    target = (root_resolved / candidate).resolve()
    if not target.is_relative_to(root_resolved):
        raise HTTPException(status_code=400, detail="path escapes the root")
    if target.suffix != ".md":
        raise HTTPException(status_code=400, detail="only .md documents are supported")
    return target


def iter_md_files(root: Path) -> list[str]:
    """Every ``.md`` file under *root*, as sorted root-relative posix paths."""
    paths: list[str] = []
    if root.exists():
        for dirpath, _dirs, filenames in os.walk(root):
            base = Path(dirpath)
            for fname in filenames:
                if fname.endswith(".md"):
                    paths.append((base / fname).relative_to(root).as_posix())
    paths.sort()
    return paths


def iter_tree_nodes(root: Path) -> list[dict[str, str]]:
    """Every ``.md`` file and non-hidden subdirectory under *root*, as ``{path, type}`` dicts.

    The list is depth-first with **directories before files** at every level, both
    alphabetically sorted within their group. This matches the visual tree the shell
    renders and is stable regardless of filesystem order. Hidden directories (those
    whose name starts with ``"."``) are skipped entirely — they are not indexed and
    are not part of the vault UI.
    """
    nodes: list[dict[str, str]] = []
    if not root.exists():
        return nodes

    def _walk(directory: Path) -> None:
        try:
            entries = list(directory.iterdir())
        except PermissionError:
            return
        subdirs = sorted(
            (e for e in entries if e.is_dir() and not e.name.startswith(".")),
            key=lambda e: e.name,
        )
        files = sorted(
            (e for e in entries if e.is_file() and e.suffix == ".md"),
            key=lambda e: e.name,
        )
        # Emit dirs before files at this level, then recurse into each dir.
        for subdir in subdirs:
            nodes.append({"path": subdir.relative_to(root).as_posix(), "type": "dir"})
            _walk(subdir)
        for f in files:
            nodes.append({"path": f.relative_to(root).as_posix(), "type": "file"})

    _walk(root)
    return nodes


def safe_dir_relative(root: Path, rel: str) -> Path:
    """Resolve *rel* against *root*, refusing anything that escapes it.

    Like :func:`safe_relative` but for directories: no ``.md`` suffix
    requirement. Used for folder-CRUD operations (create, delete, move).
    Rejects absolute paths, ``..`` traversal, symlink escapes, and empty paths.
    """
    cleaned = rel.strip().replace("\\", "/")
    if not cleaned:
        raise HTTPException(status_code=400, detail="path is required")
    candidate = PurePosixPath(cleaned)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="path escapes the root")
    root_resolved = root.resolve()
    target = (root_resolved / candidate).resolve()
    if not target.is_relative_to(root_resolved):
        raise HTTPException(status_code=400, detail="path escapes the root")
    return target
