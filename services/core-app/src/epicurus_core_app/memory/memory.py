"""The memory facade — persist conversations and recall relevant past context."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from epicurus_core_app.llm.models import ChatMessage
from epicurus_core_app.memory.recall import RecallPoint, SemanticRecall
from epicurus_core_app.memory.store import (
    ConversationStore,
    MessageMeta,
    MessageRecord,
    SessionSummary,
)

_INDEXED_ROLES = {"user", "assistant"}


class MemoryItem(BaseModel):
    """A remembered snippet for the memory view — a recall point enriched from the store.

    ``id`` is the source ``agent_messages.id``; ``role``/``created_at`` come from that row
    (absent if the row is gone). ``score`` is set only for search results.
    """

    id: int
    session_id: str
    role: str = ""
    text: str
    created_at: datetime | None = None
    score: float | None = None


class Memory:
    """Conversation persistence (Postgres) plus semantic recall (Qdrant)."""

    def __init__(self, store: ConversationStore, recall: SemanticRecall) -> None:
        self._store = store
        self._recall = recall

    async def init(self) -> None:
        await self._store.init()

    async def remember(
        self,
        *,
        tenant: str,
        session_id: str,
        role: str,
        content: str,
        entity_refs: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist a message and index user/assistant turns for recall.

        ``entity_refs`` (assistant-emitted) and ``attachments`` (user-supplied) are stored
        alongside the message so the transcript can render them again (ADR-0019); neither
        is indexed for recall.
        """
        if not content:
            return
        point_id = await self._store.append(
            tenant=tenant,
            session_id=session_id,
            role=role,
            content=content,
            entity_refs=entity_refs,
            attachments=attachments,
        )
        if role in _INDEXED_ROLES:
            await self._recall.index(
                tenant=tenant, session_id=session_id, text=content, point_id=point_id
            )

    async def history(self, *, tenant: str, session_id: str) -> list[ChatMessage]:
        rows = await self._store.history(tenant=tenant, session_id=session_id)
        return [
            ChatMessage.model_validate({"role": role, "content": content}) for role, content in rows
        ]

    async def recall(self, *, tenant: str, query: str, limit: int = 4) -> list[str]:
        return await self._recall.recall(tenant=tenant, query=query, limit=limit)

    async def sessions(self, *, tenant: str) -> list[SessionSummary]:
        """The tenant's conversations, most recently active first."""
        return await self._store.sessions(tenant=tenant)

    async def messages(self, *, tenant: str, session_id: str) -> list[MessageRecord]:
        """A session's full transcript with timestamps."""
        return await self._store.messages(tenant=tenant, session_id=session_id)

    async def forget(self, *, tenant: str, session_id: str) -> int:
        """Erase a session everywhere — history rows and recall vectors."""
        removed = await self._store.delete_session(tenant=tenant, session_id=session_id)
        await self._recall.forget_session(tenant=tenant, session_id=session_id)
        return removed

    async def memories(self, *, tenant: str, limit: int = 100) -> tuple[list[MemoryItem], int]:
        """The recall corpus newest-first (up to ``limit``) plus its total size.

        What the model can pull into future chats — each snippet enriched with the source
        message's role and timestamp. ``total`` lets the UI show how much isn't shown.
        """
        points = await self._recall.list_points(tenant=tenant, limit=limit)
        total = await self._recall.count(tenant=tenant)
        meta = await self._store.metadata_for(tenant=tenant, ids=[p.id for p in points])
        return [self._to_item(point, meta) for point in points], total

    async def search_memory(
        self, *, tenant: str, query: str, limit: int = 20
    ) -> tuple[list[MemoryItem], int]:
        """What recall surfaces for ``query`` — the same ranking a chat turn would get."""
        hits = await self._recall.search(tenant=tenant, query=query, limit=limit)
        total = await self._recall.count(tenant=tenant)
        meta = await self._store.metadata_for(tenant=tenant, ids=[h.id for h in hits])
        return [self._to_item(hit, meta, score=hit.score) for hit in hits], total

    async def forget_memory(self, *, tenant: str, point_id: int) -> int:
        """Forget one snippet so it stops being recalled (the source message is kept)."""
        return await self._recall.forget_point(tenant=tenant, point_id=point_id)

    @staticmethod
    def _to_item(
        point: RecallPoint, meta: dict[int, MessageMeta], *, score: float | None = None
    ) -> MemoryItem:
        info = meta.get(point.id)
        return MemoryItem(
            id=point.id,
            session_id=point.session_id,
            text=point.text,
            role=info.role if info else "",
            created_at=info.created_at if info else None,
            score=score,
        )
