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
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

__all__ = [
    "KNOWLEDGE_KIND",
    "SOURCE_DOC",
    "SOURCE_NOTE",
    "decode_ref",
    "doc_title",
    "encode_ref",
    "safe_dir_relative",
    "safe_project",
    "safe_relative",
    "safe_vault_dir_rel",
    "safe_vault_rel",
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


def safe_vault_rel(rel: str) -> str:
    """Validate *rel* as a vault-relative ``.md`` path; return it as a clean posix string.

    The **read-path** counterpart to :func:`safe_relative`. Where that resolves against a
    real on-disk root and returns a ``Path``, this is filesystem-independent: the vault is
    read through the core file API (ADR-0064), which enforces its own containment, so the
    module holds no ``/data`` mount to resolve against. Rejects empty, absolute,
    ``..``-traversal, and non-``.md`` paths (400), then hands the :class:`~reader.VaultReader`
    a clean ``rel`` it maps to the core key.
    """
    return _clean_vault_rel(rel, require_md=True)


def safe_vault_dir_rel(rel: str) -> str:
    """Like :func:`safe_vault_rel`, but without the ``.md`` requirement (directories)."""
    return _clean_vault_rel(rel, require_md=False)


def _clean_vault_rel(rel: str, *, require_md: bool) -> str:
    cleaned = rel.strip().replace("\\", "/")
    if not cleaned:
        raise HTTPException(status_code=400, detail="path is required")
    candidate = PurePosixPath(cleaned)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="path escapes the root")
    if require_md and candidate.suffix != ".md":
        raise HTTPException(status_code=400, detail="only .md documents are supported")
    return candidate.as_posix()


def safe_project(root: Path, name: str) -> Path:
    """Resolve a project/knowledge-base *name* to a single top-level folder under *root*.

    A project is exactly one path segment: no separators, no traversal, and no ``.``/``_``
    prefix (``_`` is reserved for virtual scopes). 400 on anything else. Returns the
    would-be directory path (the caller checks existence).
    """
    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="knowledge base name is required")
    if (
        "/" in cleaned
        or "\\" in cleaned
        or ".." in cleaned
        or cleaned.startswith(".")
        or cleaned.startswith("_")
    ):
        raise HTTPException(status_code=400, detail=f"invalid knowledge base name: {name!r}")
    root_resolved = root.resolve()
    target = (root_resolved / cleaned).resolve()
    if target.parent != root_resolved:
        raise HTTPException(status_code=400, detail=f"invalid knowledge base name: {name!r}")
    return target


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
