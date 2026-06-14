"""Heading-aware markdown chunker for notes.

Splits a note at ATX headings (``#`` through ``######``) so each chunk covers one
self-contained section. The intro block (text before the first heading) is always
included as a chunk with an empty heading string. Large sections are further split
at blank-line paragraph boundaries to stay within *max_chars*; no chunk is split
mid-paragraph.

This mirrors the knowledge module's chunker — generic markdown splitting kept local
to the module (each module owns its data plane). If a third module needs it, promote
it to ``epicurus-core`` rather than copying a third time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ATX headings: optional leading whitespace, 1-6 ``#`` chars, a space, the text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    """One indexable unit from a note."""

    heading: str
    text: str
    index: int


def chunk_note(content: str, max_chars: int = 2000) -> list[Chunk]:
    """Split *content* into heading-based chunks, each at most *max_chars* long.

    1. Split at ATX headings to get raw sections.
    2. Any section over *max_chars* is further split at blank-line boundaries
       (never mid-paragraph).
    3. Whitespace-only chunks are dropped.

    Returns a flat list of :class:`Chunk` objects, index 0 first.
    """
    raw = _split_at_headings(content)
    chunks: list[Chunk] = []
    for heading, body in raw:
        for piece in _split_large_section(heading, body, max_chars):
            if piece.strip():
                chunks.append(Chunk(heading=heading, text=piece.strip(), index=len(chunks)))
    return chunks


def _split_at_headings(content: str) -> list[tuple[str, str]]:
    """Return ``[(heading, body), ...]`` split at ATX heading boundaries.

    The intro section (before the first heading) carries heading ``""``.
    """
    sections: list[tuple[str, str]] = []
    pos = 0
    current_heading = ""
    for match in _HEADING_RE.finditer(content):
        sections.append((current_heading, content[pos : match.start()]))
        current_heading = match.group(2).strip()
        pos = match.end()
    sections.append((current_heading, content[pos:]))
    return sections


def _split_large_section(heading: str, body: str, max_chars: int) -> list[str]:
    """Split *body* at blank-line boundaries until each piece fits in *max_chars*.

    The heading (if any) is prepended to the first sub-chunk so a chunk always
    knows its parent heading.
    """
    prefix = f"{'#' * 2} {heading}\n\n" if heading else ""
    full = prefix + body
    if len(full) <= max_chars:
        return [full]

    paragraphs = re.split(r"\n{2,}", body)
    pieces: list[str] = []
    current_parts: list[str] = []
    current_len = len(prefix)

    for para in paragraphs:
        addition = len(para) + 2  # +2 for the blank-line separator
        if current_parts and current_len + addition > max_chars:
            joined = (
                (prefix + "\n\n".join(current_parts)) if not pieces else "\n\n".join(current_parts)
            )
            pieces.append(joined)
            current_parts = [para]
            current_len = len(para)
            prefix = ""  # only the first piece carries the heading prefix
        else:
            current_parts.append(para)
            current_len += addition

    if current_parts:
        joined = (prefix + "\n\n".join(current_parts)) if not pieces else "\n\n".join(current_parts)
        pieces.append(joined)

    return pieces or [full]
