"""Expand a turn's attachments into agent context (ADR-0019).

The user can attach context to a message — an uploaded ``file``, another ``chat``, or a
``module`` entity. This resolves each into a short text block the agent prepends to the
turn so the model can use it. Resolution is best-effort: a failing attachment is skipped,
never fatal.

An image ``file`` attachment is the one exception (#633): it never decodes to text (that
would just be replacement-character noise) — it resolves to an :class:`ImagePart` instead,
which the agent attaches directly to the user message as multimodal content, gated on the
selected model's vision capability.

A PDF ``file`` attachment is a second, related exception (#738): a naive UTF-8 decode of
PDF bytes is the same replacement-character noise images used to produce, just as text
rather than as an obviously-binary image. :mod:`document_extraction` reads it properly;
an encrypted or scanned (image-only) PDF renders an honest metadata block instead — never
mojibake, and never a silently-dropped attachment either. Any *other* file whose decode
turns out mostly-replacement-character (a zip renamed ``.bin``, some other unlabelled
binary) gets the same honest-block treatment, for the same reason.
"""

from __future__ import annotations

import base64

from pydantic import BaseModel

from epicurus_core import Attachment, get_logger
from epicurus_core_app.agent.document_extraction import extract_pdf, is_mostly_binary
from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.memory.store import AttachmentStore
from epicurus_core_app.modules import ModuleRegistry

log = get_logger("epicurus_core_app.agent.attachments")

_EXCERPT_CHARS = 4000
_TRANSCRIPT_MESSAGES = 20
_PDF_CONTENT_TYPE = "application/pdf"


def _excerpt(text: str, limit: int = _EXCERPT_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


def _metadata_block(*, title: str, content_type: str, size: int) -> str:
    """The honest fallback for a file with no text to show — name, type, size, never noise."""
    return f"[file: {title} — {content_type}, {size:,} bytes, not extractable as text]"


def _render_pdf(data: bytes, *, title: str) -> str:
    """A PDF's extracted text (page-marked, already bounded), or an honest metadata block."""
    doc = extract_pdf(data)
    if not doc.has_text:
        if doc.page_count:
            pages = f"{doc.page_count} page{'s' if doc.page_count != 1 else ''}"
            return f"[file: {title} — PDF, {pages}, no extractable text]"
        return f"[file: {title} — PDF, no extractable text]"
    return f"[file: {title}]\n{doc.text}"


class ImagePart(BaseModel):
    """One resolved image attachment, ready to ride a chat message's multimodal content."""

    mime: str
    data_b64: str
    title: str = ""


class ExpandedAttachments(BaseModel):
    """The result of resolving a turn's attachments (#633).

    ``text`` is the joined text blocks for the leading system preamble (unchanged
    behavior); ``images`` are resolved separately — the caller decides whether the
    selected model can take them before ever attaching them to a message.
    """

    text: str = ""
    images: list[ImagePart] = []


class AttachmentExpander:
    """Resolves attachments to text/images the agent injects into a turn."""

    def __init__(self, *, store: AttachmentStore, memory: Memory, registry: ModuleRegistry) -> None:
        self._store = store
        self._memory = memory
        self._registry = registry

    async def expand(self, attachments: list[Attachment], *, tenant: str) -> ExpandedAttachments:
        """Resolve every attachment to a text block or image (skip failures)."""
        blocks: list[str] = []
        images: list[ImagePart] = []
        for att in attachments:
            try:
                block, image = await self._one(att, tenant=tenant)
            except Exception as exc:  # one bad attachment must not break the turn
                log.warning("attachment expansion failed", att_id=att.att_id, error=str(exc))
                block, image = None, None
            if block:
                blocks.append(block)
            if image:
                images.append(image)
        return ExpandedAttachments(text="\n\n".join(blocks), images=images)

    async def _one(self, att: Attachment, *, tenant: str) -> tuple[str | None, ImagePart | None]:
        if att.source == "file":
            row = await self._store.get(tenant=tenant, att_id=att.att_id)
            if row is None:
                return None, None
            title = att.title or row.title
            if row.kind.startswith("image/"):
                data_b64 = base64.b64encode(row.content).decode("ascii")
                image = ImagePart(mime=row.kind, data_b64=data_b64, title=title)
                return None, image
            if row.kind == _PDF_CONTENT_TYPE:
                return _render_pdf(row.content, title=title), None
            text = row.content.decode("utf-8", errors="replace")
            if is_mostly_binary(text):
                return _metadata_block(
                    title=title, content_type=row.kind, size=len(row.content)
                ), None
            return f"[file: {title}]\n{_excerpt(text)}", None
        if att.source == "chat" and att.ref_id:
            messages = await self._memory.messages(tenant=tenant, session_id=att.ref_id)
            recent = messages[-_TRANSCRIPT_MESSAGES:]
            transcript = "\n".join(f"{m.role}: {m.content}" for m in recent)
            return f"[chat: {att.title or att.ref_id}]\n{_excerpt(transcript)}", None
        if att.source == "module" and att.module and att.ref_id:
            data = await self._registry.resolve_attachment(att.module, att.ref_id)
            excerpt = data.get("excerpt") or data.get("text") or ""
            return f"[{att.title or att.module}]\n{_excerpt(str(excerpt))}", None
        return None, None
