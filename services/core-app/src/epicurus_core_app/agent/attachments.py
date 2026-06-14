"""Expand a turn's attachments into agent context (ADR-0019).

The user can attach context to a message — an uploaded ``file``, another ``chat``, or a
``module`` entity. This resolves each into a short text block the agent prepends to the
turn so the model can use it. Resolution is best-effort: a failing attachment is skipped,
never fatal.
"""

from __future__ import annotations

from epicurus_core import Attachment, get_logger
from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.memory.store import AttachmentStore
from epicurus_core_app.modules import ModuleRegistry

log = get_logger("epicurus_core_app.agent.attachments")

_EXCERPT_CHARS = 4000
_TRANSCRIPT_MESSAGES = 20


def _excerpt(text: str, limit: int = _EXCERPT_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


class AttachmentExpander:
    """Resolves attachments to text the agent injects into a turn."""

    def __init__(self, *, store: AttachmentStore, memory: Memory, registry: ModuleRegistry) -> None:
        self._store = store
        self._memory = memory
        self._registry = registry

    async def expand(self, attachments: list[Attachment], *, tenant: str) -> str:
        """Resolve every attachment to a text block; join them (skip failures)."""
        blocks: list[str] = []
        for att in attachments:
            try:
                block = await self._one(att, tenant=tenant)
            except Exception as exc:  # one bad attachment must not break the turn
                log.warning("attachment expansion failed", att_id=att.att_id, error=str(exc))
                block = None
            if block:
                blocks.append(block)
        return "\n\n".join(blocks)

    async def _one(self, att: Attachment, *, tenant: str) -> str | None:
        if att.source == "file":
            row = await self._store.get(tenant=tenant, att_id=att.att_id)
            if row is None:
                return None
            text = row.content.decode("utf-8", errors="replace")
            return f"[file: {att.title or row.title}]\n{_excerpt(text)}"
        if att.source == "chat" and att.ref_id:
            messages = await self._memory.messages(tenant=tenant, session_id=att.ref_id)
            recent = messages[-_TRANSCRIPT_MESSAGES:]
            transcript = "\n".join(f"{m.role}: {m.content}" for m in recent)
            return f"[chat: {att.title or att.ref_id}]\n{_excerpt(transcript)}"
        if att.source == "module" and att.module and att.ref_id:
            data = await self._registry.resolve_attachment(att.module, att.ref_id)
            excerpt = data.get("excerpt") or data.get("text") or ""
            return f"[{att.title or att.module}]\n{_excerpt(str(excerpt))}"
        return None
