"""Heading-aware markdown chunker for Obsidian vault notes.

Splits a note at ATX headings (``#`` through ``######``) so each chunk covers
one self-contained section.  The intro block (text before the first heading) is
always included as a chunk with an empty heading string.

Large sections are further split at blank-line paragraph boundaries to stay
within *max_chars* characters; no chunk is split mid-word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches ATX headings: optional leading whitespace, 1-6 ``#`` chars, a space,
# then the heading text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    """One indexable unit from a vault note."""

    heading: str
    text: str
    index: int


def chunk_note(content: str, max_chars: int = 2000) -> list[Chunk]:
    """Split *content* into heading-based chunks, each at most *max_chars* long.

    Steps:
    1. Split at ATX headings to get raw sections.
    2. Any section that exceeds *max_chars* is further split at blank-line
       paragraph boundaries (hard limit: never splits mid-paragraph).
    3. Empty chunks (whitespace only) are dropped.

    Returns a flat list of :class:`Chunk` objects, index 0 first.
    """
    raw = _split_at_headings(content)
    chunks: list[Chunk] = []
    for heading, body in raw:
        pieces = _split_large_section(heading, body, max_chars)
        for piece in pieces:
            if piece.strip():
                chunks.append(Chunk(heading=heading, text=piece.strip(), index=len(chunks)))
    return chunks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _split_at_headings(content: str) -> list[tuple[str, str]]:
    """Return ``[(heading, body), ...]`` by splitting at ATX heading boundaries.

    The intro section (before the first heading) is returned with heading ``""``.
    """
    sections: list[tuple[str, str]] = []
    pos = 0
    current_heading = ""
    for match in _HEADING_RE.finditer(content):
        start = match.start()
        body = content[pos:start]
        sections.append((current_heading, body))
        current_heading = match.group(2).strip()
        pos = match.end()
    # Remainder after the last heading.
    sections.append((current_heading, content[pos:]))
    return sections


def _split_large_section(heading: str, body: str, max_chars: int) -> list[str]:
    """Split *body* at blank-line boundaries until each piece fits in *max_chars*.

    The heading text (if any) is prepended to the first sub-chunk of each
    section so a search result always knows its parent heading.
    """
    prefix = f"{'#' * 2} {heading}\n\n" if heading else ""
    full = prefix + body
    if len(full) <= max_chars:
        return [full]

    # Paragraph boundaries: one or more blank lines.
    paragraphs = re.split(r"\n{2,}", body)
    pieces: list[str] = []
    current_parts: list[str] = []
    current_len = len(prefix)

    for para in paragraphs:
        addition = len(para) + 2  # +2 for the blank-line separator
        if current_parts and current_len + addition > max_chars:
            body = "\n\n".join(current_parts)
            joined = (prefix + body) if not pieces else body
            pieces.append(joined)
            current_parts = [para]
            current_len = len(para)
            prefix = ""  # only the first piece carries the heading prefix
        else:
            current_parts.append(para)
            current_len += addition

    if current_parts:
        joined = prefix + "\n\n".join(current_parts) if not pieces else "\n\n".join(current_parts)
        pieces.append(joined)

    return pieces or [full]
