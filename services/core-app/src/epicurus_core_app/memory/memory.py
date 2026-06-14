"""The memory facade — persist conversations and recall relevant past context."""

from __future__ import annotations

from typing import Any

from epicurus_core_app.llm.models import ChatMessage
from epicurus_core_app.memory.recall import SemanticRecall
from epicurus_core_app.memory.store import ConversationStore, MessageRecord, SessionSummary

_INDEXED_ROLES = {"user", "assistant"}


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
    ) -> None:
        """Persist a message and index user/assistant turns for recall.

        ``entity_refs`` (assistant-emitted, ADR-0019) is stored alongside the message so
        the transcript can render the chips again; it is not indexed for recall.
        """
        if not content:
            return
        point_id = await self._store.append(
            tenant=tenant,
            session_id=session_id,
            role=role,
            content=content,
            entity_refs=entity_refs,
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
