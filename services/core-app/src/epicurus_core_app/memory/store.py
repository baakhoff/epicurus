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
    inspect,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from epicurus_core import Attachment, EntityRef
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
        """Idempotently add post-v0.2 JSON columns to an existing table.

        There is no migration framework (the store uses ``create_all``), so on a
        deployment that predates these columns we add them in place. The column type is
        compiled per-dialect (``JSON`` on Postgres, TEXT-backed on SQLite), so this is
        portable across prod and the tests' SQLite.
        """
        inspector = inspect(sync_conn)
        existing = {col["name"] for col in inspector.get_columns(StoredMessage.__tablename__)}
        for name in _ADDED_JSON_COLUMNS:
            if name not in existing:
                type_sql = StoredMessage.__table__.c[name].type.compile(dialect=sync_conn.dialect)
                sync_conn.exec_driver_sql(
                    f"ALTER TABLE {StoredMessage.__tablename__} ADD COLUMN {name} {type_sql}"
                )

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

    async def update_content(self, *, tenant: str, message_id: int, content: str) -> None:
        """Replace one message's content in place (tenant-scoped) — backs an edited turn."""
        async with self._session() as session:
            await session.execute(
                update(StoredMessage)
                .where(StoredMessage.tenant == tenant, StoredMessage.id == message_id)
                .values(content=content)
            )
            await session.commit()

    async def truncate_after(self, *, tenant: str, session_id: str, after_id: int) -> list[int]:
        """Delete the session's messages with ``id > after_id``; returns the removed ids.

        Drops everything inserted after the anchor message — the assistant answer (and any
        trailing turns) when regenerating or editing. The caller drops the matching recall
        points. Returns the ids so recall stays consistent with history.
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
