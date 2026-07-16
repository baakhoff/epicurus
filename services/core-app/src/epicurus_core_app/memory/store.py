"""Conversation persistence — append-only message history in Postgres (tenant-scoped)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, Field
from sqlalchemy import (
    JSON,
    CursorResult,
    DateTime,
    LargeBinary,
    String,
    Text,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import Attachment, EntityRef
from epicurus_core.db import ensure_columns
from epicurus_core_app.agent.activity import MessageActivity

# JSON columns added to agent_messages after the table's first release (v0.2). On an
# existing deployment these are added in place at init (the store has no migration
# framework — see ``ConversationStore._ensure_columns``). ``activity`` joined them in v0.19
# (ADR-0041: the assistant turn's persisted thinking + tool steps).
_ADDED_JSON_COLUMNS = ("entity_refs", "attachments", "activity")


class SessionSummary(BaseModel):
    """One conversation in the sessions list, newest activity first."""

    id: str
    title: str
    message_count: int
    last_at: datetime


class MessageRecord(BaseModel):
    """A persisted message with its timestamp (the UI's transcript shape)."""

    # The message's stable id — what the client names to edit a turn other than the last
    # one (#552). Opaque to the UI: it addresses a row the tenant+session already scopes.
    id: int
    role: str
    content: str
    created_at: datetime
    # Entities the assistant referenced this turn (ADR-0019) — rendered as chips.
    entity_refs: list[EntityRef] = Field(default_factory=list)
    # Context the user attached to this message (ADR-0019) — rendered as pills.
    attachments: list[Attachment] = Field(default_factory=list)
    # The assistant turn's process — thinking + tool steps (ADR-0041) — rendered as the
    # folded activity timeline. None on user messages and on pre-v0.19 assistant rows.
    activity: MessageActivity | None = None


class MessageMeta(BaseModel):
    """Per-message metadata looked up by id — enriches a recall snippet (role + when)."""

    role: str
    created_at: datetime


class MessageHit(BaseModel):
    """A message matched by a content search, with the session + timestamp it came from (#523).

    Backs the agent's ``memory_search`` deliberate recall over past conversations — unlike
    :class:`MessageMeta` (metadata only) it carries the matching text so the caller can build a
    snippet, and the ``session_id`` so a hit can name the conversation it belongs to.
    """

    session_id: str
    role: str
    content: str
    created_at: datetime


class Base(DeclarativeBase):
    pass


class StoredMessage(Base):
    """One persisted message in a conversation."""

    __tablename__ = "agent_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Assistant-emitted entity references for this message (ADR-0019); null for old rows.
    entity_refs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # User-supplied attachments for this message (ADR-0019); null for old rows.
    attachments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # The assistant turn's thinking + tool steps (ADR-0041); null for user/old rows.
    activity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class StoredAttachment(Base):
    """An uploaded file's bytes, held core-side under ``att_id`` (ADR-0019).

    The agent reads these to expand a ``file`` attachment into turn context. In Phase 3.8
    the storage module also persists uploads (#135); this is the core-side handle.
    """

    __tablename__ = "agent_attachments"

    att_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    kind: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationStore:
    """Append-only conversation history in Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create the schema, then add any columns introduced after first release."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(self._ensure_columns)

    @staticmethod
    def _ensure_columns(sync_conn: Connection) -> None:
        """Reconcile columns added after first release via the shared additive helper (#249).

        ``entity_refs`` / ``attachments`` (ADR-0019) and ``activity`` (ADR-0041) are nullable
        JSON columns added after v0.2; they are added in place on an older table. See
        :func:`epicurus_core.db.ensure_columns`.
        """
        ensure_columns(sync_conn, StoredMessage.__table__, _ADDED_JSON_COLUMNS)

    async def append(
        self,
        *,
        tenant: str,
        session_id: str,
        role: str,
        content: str,
        entity_refs: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        activity: dict[str, Any] | None = None,
    ) -> int:
        """Persist a message; returns its id (used as the recall point id)."""
        async with self._session() as session:
            message = StoredMessage(
                tenant=tenant,
                session_id=session_id,
                role=role,
                content=content,
                entity_refs=entity_refs,
                attachments=attachments,
                activity=activity,
            )
            session.add(message)
            await session.commit()
            return message.id

    async def history(self, *, tenant: str, session_id: str) -> list[tuple[str, str]]:
        """Return the ``(role, content)`` messages of a session, oldest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(StoredMessage)
                .where(StoredMessage.tenant == tenant, StoredMessage.session_id == session_id)
                .order_by(StoredMessage.created_at, StoredMessage.id)
            )
            return [(message.role, message.content) for message in rows]

    async def distinct_tenants(self) -> list[str]:
        """Every tenant that has conversation history — the set to synthesize profiles for (#527).

        Facts (the synthesis input) are only ever written inside a turn, which persists messages,
        so a tenant with facts always has rows here; this is the tenant-first fan-out source
        (constraint #1) for the nightly profile job, even though v1 has a single tenant.
        """
        async with self._session() as session:
            rows = await session.scalars(select(StoredMessage.tenant).distinct())
            return list(rows)

    async def sessions(self, *, tenant: str) -> list[SessionSummary]:
        """Summarize the tenant's conversations, most recently active first.

        A session's title is its first stored message (the opening user turn).
        """
        async with self._session() as session:
            aggregate = (
                select(
                    StoredMessage.session_id,
                    func.count().label("message_count"),
                    func.max(StoredMessage.created_at).label("last_at"),
                    func.min(StoredMessage.id).label("first_id"),
                )
                .where(StoredMessage.tenant == tenant)
                .group_by(StoredMessage.session_id)
                .order_by(func.max(StoredMessage.created_at).desc())
            )
            rows = (await session.execute(aggregate)).all()
            titles: dict[int, str] = {}
            first_ids = [row.first_id for row in rows]
            if first_ids:
                firsts = await session.scalars(
                    select(StoredMessage).where(StoredMessage.id.in_(first_ids))
                )
                titles = {message.id: message.content for message in firsts}
            return [
                SessionSummary(
                    id=row.session_id,
                    title=titles.get(row.first_id, "").strip()[:80],
                    message_count=row.message_count,
                    last_at=row.last_at,
                )
                for row in rows
            ]

    async def messages(self, *, tenant: str, session_id: str) -> list[MessageRecord]:
        """Return a session's full transcript with timestamps, oldest first."""
        async with self._session() as session:
            rows = await session.scalars(
                select(StoredMessage)
                .where(StoredMessage.tenant == tenant, StoredMessage.session_id == session_id)
                .order_by(StoredMessage.created_at, StoredMessage.id)
            )
            return [
                MessageRecord(
                    id=m.id,
                    role=m.role,
                    content=m.content,
                    created_at=m.created_at,
                    entity_refs=[EntityRef.model_validate(r) for r in (m.entity_refs or [])],
                    attachments=[Attachment.model_validate(a) for a in (m.attachments or [])],
                    activity=MessageActivity.model_validate(m.activity) if m.activity else None,
                )
                for m in rows
            ]

    async def metadata_for(self, *, tenant: str, ids: list[int]) -> dict[int, MessageMeta]:
        """Look up role + created_at for a set of message ids (tenant-scoped).

        Backs the memory view, which gets snippet ids + text from the recall index and joins
        them back to ``agent_messages`` for display metadata.
        """
        if not ids:
            return {}
        async with self._session() as session:
            rows = await session.execute(
                select(StoredMessage.id, StoredMessage.role, StoredMessage.created_at).where(
                    StoredMessage.tenant == tenant, StoredMessage.id.in_(ids)
                )
            )
            return {
                row.id: MessageMeta(role=row.role, created_at=row.created_at) for row in rows.all()
            }

    async def search_messages(self, *, tenant: str, query: str, limit: int = 8) -> list[MessageHit]:
        """Search a tenant's message content, most-recent first, capped at ``limit`` (#523).

        Backs the agent's ``memory_search`` built-in — a deliberate look back over past
        conversations. Uses a portable case-insensitive substring match (``ILIKE`` → native on
        Postgres, ``lower() LIKE lower()`` on the tests' SQLite) so one query runs on both; a
        Postgres full-text index is a future optimization (a single operator's chat history is
        small). ``tenant`` scopes the search — recall crosses sessions, so the filter is a
        privacy boundary, never optional. A blank query matches nothing (rather than everything).
        """
        cleaned = query.strip()
        if not cleaned:
            return []
        # Escape the LIKE metacharacters so a query containing % or _ matches literally.
        escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        async with self._session() as session:
            rows = await session.scalars(
                select(StoredMessage)
                .where(
                    StoredMessage.tenant == tenant,
                    StoredMessage.content.ilike(pattern, escape="\\"),
                )
                .order_by(StoredMessage.created_at.desc(), StoredMessage.id.desc())
                .limit(limit)
            )
            return [
                MessageHit(
                    session_id=m.session_id,
                    role=m.role,
                    content=m.content,
                    created_at=m.created_at,
                )
                for m in rows
            ]

    async def session_titles(self, *, tenant: str, session_ids: list[str]) -> dict[str, str]:
        """Map each of ``session_ids`` to its title — its opening message (tenant-scoped).

        Mirrors :meth:`sessions`' title rule (the first stored message) for a known subset —
        the sessions a memory search matched — so a hit can name its conversation without
        loading every session. One group-by plus one fetch, never N+1.
        """
        if not session_ids:
            return {}
        async with self._session() as session:
            firsts = await session.execute(
                select(func.min(StoredMessage.id).label("first_id"))
                .where(
                    StoredMessage.tenant == tenant,
                    StoredMessage.session_id.in_(session_ids),
                )
                .group_by(StoredMessage.session_id)
            )
            first_ids = [row.first_id for row in firsts.all()]
            if not first_ids:
                return {}
            rows = await session.execute(
                select(StoredMessage.session_id, StoredMessage.content).where(
                    StoredMessage.id.in_(first_ids)
                )
            )
            return {row.session_id: (row.content or "").strip() for row in rows.all()}

    async def delete_session(self, *, tenant: str, session_id: str) -> int:
        """Delete a session's messages; returns how many were removed."""
        async with self._session() as session:
            result = await session.execute(
                delete(StoredMessage).where(
                    StoredMessage.tenant == tenant, StoredMessage.session_id == session_id
                )
            )
            await session.commit()
            # DELETE always returns a CursorResult; the ORM types it as plain Result.
            return cast("CursorResult[Any]", result).rowcount or 0

    async def last_message_id(
        self, *, tenant: str, session_id: str, role: str | None = None
    ) -> int | None:
        """The id of the session's most recent message (optionally of a given ``role``).

        ``id`` is autoincrement, so "highest id" is the last-inserted message — the reliable
        anchor for regenerate/edit (truncate everything after the last user turn). Returns
        ``None`` when the session has no matching message.
        """
        async with self._session() as session:
            stmt = select(StoredMessage.id).where(
                StoredMessage.tenant == tenant, StoredMessage.session_id == session_id
            )
            if role is not None:
                stmt = stmt.where(StoredMessage.role == role)
            return cast(
                "int | None", await session.scalar(stmt.order_by(StoredMessage.id.desc()).limit(1))
            )

    async def message_role(self, *, tenant: str, session_id: str, message_id: int) -> str | None:
        """The role of one message, or ``None`` if it isn't in this tenant's session (#552).

        The edit anchor's validation primitive: a client naming an arbitrary ``message_id``
        (editing a turn other than the last) must be checked *before* anything is revised or
        truncated, and the check has to prove the row is in **this** conversation — not merely
        in this tenant, which would let one session's edit rewrite another's history.
        """
        async with self._session() as session:
            return cast(
                "str | None",
                await session.scalar(
                    select(StoredMessage.role).where(
                        StoredMessage.tenant == tenant,
                        StoredMessage.session_id == session_id,
                        StoredMessage.id == message_id,
                    )
                ),
            )

    async def update_content(
        self, *, tenant: str, session_id: str, message_id: int, content: str
    ) -> None:
        """Replace one message's content in place — backs an edited turn.

        Scoped to tenant **and** session: ``message_id`` is client-supplied since #552, so the
        session predicate keeps a stray id from rewriting another conversation even if a caller
        skips the :meth:`message_role` check.
        """
        async with self._session() as session:
            await session.execute(
                update(StoredMessage)
                .where(
                    StoredMessage.tenant == tenant,
                    StoredMessage.session_id == session_id,
                    StoredMessage.id == message_id,
                )
                .values(content=content)
            )
            await session.commit()

    async def truncate_after(self, *, tenant: str, session_id: str, after_id: int) -> list[int]:
        """Delete the session's messages with ``id > after_id``; returns the removed ids.

        Drops everything inserted after the anchor message — the assistant answer (and any
        trailing turns) when regenerating, or the whole tail behind an edited turn (#552).
        Nothing else has to be reaped to keep recall consistent: messages are not a recall
        corpus (ADR-0045), and ``memory_search`` reads these rows live, so a deleted row stops
        being findable by construction. The removed ids are returned for the caller to report.
        """
        async with self._session() as session:
            ids = list(
                await session.scalars(
                    select(StoredMessage.id).where(
                        StoredMessage.tenant == tenant,
                        StoredMessage.session_id == session_id,
                        StoredMessage.id > after_id,
                    )
                )
            )
            if ids:
                await session.execute(delete(StoredMessage).where(StoredMessage.id.in_(ids)))
                await session.commit()
            return ids


class AttachmentStore:
    """Core-side storage for uploaded attachment bytes, tenant-scoped (ADR-0019).

    Shares ``Base`` with :class:`ConversationStore`, so its table is created by
    ``ConversationStore.init`` (``create_all``). The agent reads these to expand a
    ``file`` attachment into the turn's context.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def save(self, *, tenant: str, kind: str, title: str, content: bytes) -> str:
        """Persist an uploaded file; returns the new ``att_id``."""
        att_id = uuid.uuid4().hex
        async with self._session() as session:
            session.add(
                StoredAttachment(
                    att_id=att_id, tenant=tenant, kind=kind, title=title, content=content
                )
            )
            await session.commit()
        return att_id

    async def get(self, *, tenant: str, att_id: str) -> StoredAttachment | None:
        """Fetch a stored attachment by id, scoped to the tenant (None if absent)."""
        async with self._session() as session:
            row = await session.get(StoredAttachment, att_id)
            return row if row is not None and row.tenant == tenant else None
