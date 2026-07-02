"""Hover-card resolver for cited knowledge documents (#143, ADR-0019).

When the agent cites a knowledge document — a ``knowledge_search`` result rendered as a
chat entity-reference chip — hovering the chip asks the core for a hover-card, and the core
proxies ``GET /resolve/{kind}/{ref_id}`` here. This module supplies data only; the core
renders the uniform :class:`~epicurus_core.HoverCard` envelope (title, description, detail
rows, and an optional open link).

For a **vault note** the link deep-links into the Knowledge editor page
(``/m/knowledge/vault?doc=…``) so a click opens the document. The read-only **platform
docs** have no editor page, so they resolve to a card without a link.
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import quote

from fastapi import APIRouter, HTTPException

from epicurus_core import HoverCard, HoverCardDetail, HoverCardLink, get_logger
from epicurus_knowledge.db import DocIndex, NoteIndex
from epicurus_knowledge.pages import VAULT_PAGE_ID
from epicurus_knowledge.reader import VaultReader
from epicurus_knowledge.refs import (
    KNOWLEDGE_KIND,
    SOURCE_DOC,
    decode_ref,
    doc_title,
    safe_vault_rel,
)

log = get_logger("knowledge.resolver")

_DESCRIPTION_CHARS = 220
_MAX_TAGS = 8


class _IndexLedger(Protocol):
    """The slice of NoteIndex/DocIndex the resolver needs — a per-path index time."""

    async def indexed_at(self, *, tenant: str, note_path: str) -> str | None: ...


class KnowledgeResolver:
    """Builds the hover-card for a cited vault note or platform-docs page."""

    def __init__(
        self,
        *,
        vault_reader: VaultReader,
        docs_reader: VaultReader,
        note_index: NoteIndex,
        doc_index: DocIndex,
        tenant: str,
    ) -> None:
        self._vault_reader = vault_reader
        self._docs_reader = docs_reader
        self._note_index = note_index
        self._doc_index = doc_index
        self._tenant = tenant

    async def resolve(self, kind: str, ref_id: str) -> HoverCard:
        """Resolve a ``kind="knowledge"`` reference to its hover-card; 404 otherwise."""
        if kind != KNOWLEDGE_KIND:
            raise HTTPException(status_code=404, detail=f"unknown entity kind: {kind}")
        source, path = decode_ref(ref_id)

        is_doc = source == SOURCE_DOC
        reader = self._docs_reader if is_doc else self._vault_reader
        index: _IndexLedger = self._doc_index if is_doc else self._note_index
        display_path = f"docs/{path}" if is_doc else path

        text = await reader.read_text(safe_vault_rel(path))
        if text is None:
            raise HTTPException(status_code=404, detail=f"no such document: {display_path}")

        details = [HoverCardDetail(label="Path", value=display_path)]
        tags = _frontmatter_tags(text)
        if tags:
            details.append(HoverCardDetail(label="Tags", value=", ".join(tags)))
        indexed = await self._safe_indexed_at(index, path)
        if indexed:
            details.append(HoverCardDetail(label="Last indexed", value=indexed))

        # Vault notes open in the Knowledge editor page; read-only platform docs have none.
        href = (
            None
            if is_doc
            else HoverCardLink(
                label="Open in Knowledge",
                url=f"/m/knowledge/{VAULT_PAGE_ID}?doc={quote(path)}",
            )
        )
        return HoverCard(
            title=doc_title(path),
            description=_description(text),
            details=details,
            href=href,
        )

    async def _safe_indexed_at(self, index: _IndexLedger, path: str) -> str | None:
        """The doc's last-indexed time, to the minute; best-effort (None if the ledger errs)."""
        try:
            raw = await index.indexed_at(tenant=self._tenant, note_path=path)
        except Exception as exc:  # a hover-card must not fail because the ledger is down
            log.debug("indexed_at lookup failed", path=path, error=str(exc))
            return None
        if not raw:
            return None
        # "2026-06-14T12:34:56.789+00:00" -> "2026-06-14 12:34"
        return raw[:16].replace("T", " ")


def _strip_frontmatter(text: str) -> str:
    """Drop a leading ``---`` YAML frontmatter block, if present."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4 :].lstrip()


def _description(text: str) -> str:
    """A one-line preview: the body (sans frontmatter), whitespace-collapsed and truncated."""
    collapsed = " ".join(_strip_frontmatter(text).split())
    if len(collapsed) <= _DESCRIPTION_CHARS:
        return collapsed
    return collapsed[:_DESCRIPTION_CHARS].rstrip() + "…"


def _frontmatter_tags(text: str) -> list[str]:
    """Best-effort YAML-frontmatter ``tags:`` extraction (inline list or block form)."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    lines = text[3:end].splitlines()
    raw: list[str] = []
    for i, line in enumerate(lines):
        if not line.strip().startswith("tags:"):
            continue
        inline = line.strip()[len("tags:") :].strip()
        if inline:  # `tags: [a, b]` or `tags: a, b`
            raw = inline.strip("[]").split(",")
        else:  # block form: subsequent `- tag` lines
            for follow in lines[i + 1 :]:
                stripped = follow.strip()
                if stripped.startswith("- "):
                    raw.append(stripped[2:])
                elif stripped:
                    break
        break
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw:
        tag = item.strip().strip("'\"").lstrip("#").strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags[:_MAX_TAGS]


def create_resolver_router(resolver: KnowledgeResolver) -> APIRouter:
    """The hover-card resolver surface the core proxies (#143, ADR-0019)."""
    router = APIRouter(tags=["resolver"])

    @router.get("/resolve/{kind}/{ref_id}", response_model=HoverCard)
    async def resolve_entity(kind: str, ref_id: str) -> HoverCard:
        return await resolver.resolve(kind, ref_id)

    return router
