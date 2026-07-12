"""The memory facade — persist conversations, recall durable facts about the user.

Two stores sit behind this: conversation history in Postgres (the verbatim transcript, per
session) and a tenant-scoped corpus of durable *facts* about the user in Qdrant
(:class:`UserFactStore`). A chat turn is grounded in the session's own history plus the
facts recall surfaces; the facts themselves are written by the agent's ``remember`` tool and
by background extraction (ADR-0045), never by dumping raw messages into the index — which is
what "memory" used to be, and why it read like a transcript instead of facts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from epicurus_core_app.llm.models import ChatMessage
from epicurus_core_app.memory.facts import UserFact, UserFactHit, UserFactStore
from epicurus_core_app.memory.store import (
    ConversationStore,
    MessageRecord,
    SessionSummary,
)


class MemoryItem(BaseModel):
    """A remembered fact for the memory view.

    ``id`` is the fact's opaque UUID; ``source`` is how it was learned (``tool`` = the agent's
    ``remember`` tool, ``auto`` = background extraction); ``score`` is set only for search
    results.
    """

    id: str
    text: str
    source: str = "auto"
    created_at: datetime | None = None
    score: float | None = None


class SessionHit(BaseModel):
    """A past-conversation match for the agent's deliberate ``memory_search`` recall (#523).

    Carries the matched message (``snippet`` + ``role``) plus the conversation it came from
    (``session_id`` + ``title`` + ``created_at``), so a hit reads with enough context for the
    model to use it — "on {date}, in '{title}', {role} said …".
    """

    session_id: str
    title: str
    role: str
    snippet: str
    created_at: datetime | None = None


class Memory:
    """Conversation persistence (Postgres) plus durable user-fact memory (Qdrant)."""

    def __init__(self, store: ConversationStore, facts: UserFactStore) -> None:
        self._store = store
        self._facts = facts

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
        activity: dict[str, Any] | None = None,
    ) -> None:
        """Persist a message to the session transcript.

        ``entity_refs`` (assistant-emitted), ``attachments`` (user-supplied) and ``activity``
        (the assistant turn's thinking + tool steps, ADR-0041) are stored alongside so the
        transcript renders them again. Messages are *not* indexed for cross-chat recall — the
        recall corpus is the user-fact store, written deliberately (the ``remember`` tool and
        background extraction), not a dump of every turn (ADR-0045).
        """
        if not content:
            return
        await self._store.append(
            tenant=tenant,
            session_id=session_id,
            role=role,
            content=content,
            entity_refs=entity_refs,
            attachments=attachments,
            activity=activity,
        )

    async def remember_fact(
        self, *, tenant: str, text: str, source: str = "auto"
    ) -> UserFact | None:
        """Save a durable fact about the user; ``None`` if it duplicates an existing one."""
        return await self._facts.save(tenant=tenant, text=text, source=source)

    async def history(self, *, tenant: str, session_id: str) -> list[ChatMessage]:
        rows = await self._store.history(tenant=tenant, session_id=session_id)
        return [
            ChatMessage.model_validate({"role": role, "content": content}) for role, content in rows
        ]

    async def recall(self, *, tenant: str, query: str, limit: int = 8) -> list[str]:
        """The agent's recall path: the text of the facts most relevant to ``query``."""
        return await self._facts.recall(tenant=tenant, query=query, limit=limit)

    async def sessions(self, *, tenant: str) -> list[SessionSummary]:
        """The tenant's conversations, most recently active first."""
        return await self._store.sessions(tenant=tenant)

    async def messages(self, *, tenant: str, session_id: str) -> list[MessageRecord]:
        """A session's full transcript with timestamps."""
        return await self._store.messages(tenant=tenant, session_id=session_id)

    async def last_user_message_id(self, *, tenant: str, session_id: str) -> int | None:
        """The id of the session's most recent user message (the regenerate/edit anchor, #302)."""
        return await self._store.last_message_id(tenant=tenant, session_id=session_id, role="user")

    async def truncate_after(self, *, tenant: str, session_id: str, after_id: int) -> int:
        """Drop the session's messages after ``after_id`` from history; returns the count (#302).

        Used by regenerate/edit to clear the stale answer (and any trailing turns) before
        re-running. Remembered facts are deliberately left intact — they belong to the user
        across chats, not to the turn that surfaced them (the same rule as :meth:`forget`).
        """
        removed = await self._store.truncate_after(
            tenant=tenant, session_id=session_id, after_id=after_id
        )
        return len(removed)

    async def revise_message(
        self, *, tenant: str, session_id: str, message_id: int, content: str
    ) -> None:
        """Replace a stored message's content in place — edit-and-re-answer (#302)."""
        await self._store.update_content(tenant=tenant, message_id=message_id, content=content)

    async def forget(self, *, tenant: str, session_id: str) -> int:
        """Erase a conversation's history rows.

        Facts the user is remembered by are deliberately left intact — they belong to the
        user across chats, not to the conversation that happened to surface them. Forget a
        single fact through the memory view instead.
        """
        return await self._store.delete_session(tenant=tenant, session_id=session_id)

    async def memories(self, *, tenant: str, limit: int = 200) -> tuple[list[MemoryItem], int]:
        """The fact corpus newest-first (up to ``limit``) plus its total size.

        What the model can pull into future chats. ``total`` lets the UI show how much
        isn't shown.
        """
        facts = await self._facts.list_facts(tenant=tenant, limit=limit)
        total = await self._facts.count(tenant=tenant)
        return [self._to_item(fact) for fact in facts], total

    async def search_memory(
        self, *, tenant: str, query: str, limit: int = 20
    ) -> tuple[list[MemoryItem], int]:
        """What recall surfaces for ``query`` — the same ranking a chat turn would get."""
        hits = await self._facts.search(tenant=tenant, query=query, limit=limit)
        total = await self._facts.count(tenant=tenant)
        return [self._to_item(hit, score=hit.score) for hit in hits], total

    async def search_sessions(self, *, tenant: str, query: str, limit: int = 5) -> list[SessionHit]:
        """Search past conversations for ``query`` — the sessions half of ``memory_search`` (#523).

        Matches message content (Postgres, no embedding round-trip) and joins each hit back to
        its conversation's title in one extra query, so the agent can deliberately look up
        something said in an earlier chat it wasn't handed. Tenant-scoped throughout.
        """
        hits = await self._store.search_messages(tenant=tenant, query=query, limit=limit)
        if not hits:
            return []
        titles = await self._store.session_titles(
            tenant=tenant, session_ids=list({hit.session_id for hit in hits})
        )
        return [
            SessionHit(
                session_id=hit.session_id,
                title=titles.get(hit.session_id, "").strip()[:80],
                role=hit.role,
                snippet=hit.content,
                created_at=hit.created_at,
            )
            for hit in hits
        ]

    async def forget_memory(self, *, tenant: str, memory_id: str) -> int:
        """Forget one fact so it stops being recalled."""
        return await self._facts.forget(tenant=tenant, fact_id=memory_id)

    @staticmethod
    def _to_item(fact: UserFact | UserFactHit, *, score: float | None = None) -> MemoryItem:
        return MemoryItem(
            id=fact.id,
            text=fact.text,
            source=fact.source,
            created_at=fact.created_at,
            score=score,
        )
