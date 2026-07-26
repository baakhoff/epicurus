"""Per-session model override — the session row's own persisted model choice (#707).

``AgentRequest.model`` (and its regenerate/edit siblings) is a per-*request* field, and
there is no dedicated "session" row anywhere to add a column to: a session is derived from
``agent_messages`` via ``GROUP BY`` (``ConversationStore.sessions``). A session-scoped model
choice needs its own small sidecar table — the same shape ``AutomationSessionStore`` already
uses for the automations-chat badge (``automations/store.py``).

Both the ``set_chat_model`` tool and an explicit picker change write here, through the same
:meth:`SessionModelStore.set` — one field, whichever wrote last wins, so the two paths never
fight (the design point the issue asked this PR to settle).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _Base(DeclarativeBase):
    pass


class _StoredSessionModel(_Base):
    """One session's persisted model override. ``session_id`` is the primary key — a row
    exists only for a session whose model was explicitly set (by the tool or the picker);
    a session absent from this table has no override and falls back to the caller's own
    default (the web's per-device picker choice)."""

    __tablename__ = "session_models"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(63), index=True)
    model: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionModelStore:
    """Tenant-scoped per-session model override (#707)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    async def get(self, *, tenant: str, session_id: str) -> str | None:
        """The session's persisted model, or ``None`` if it was never set."""
        async with self._session() as session:
            row = await session.get(_StoredSessionModel, session_id)
            if row is None or row.tenant != tenant:
                return None
            return row.model

    async def set(self, *, tenant: str, session_id: str, model: str) -> None:
        """Upsert the session's model — the one write both the tool and the picker use."""
        async with self._session() as session:
            row = await session.get(_StoredSessionModel, session_id)
            now = datetime.now(UTC)
            if row is None:
                session.add(
                    _StoredSessionModel(
                        session_id=session_id, tenant=tenant, model=model, updated_at=now
                    )
                )
            else:
                row.tenant = tenant
                row.model = model
                row.updated_at = now
            await session.commit()

    async def clear(self, *, tenant: str, session_id: str) -> None:
        """Drop the session's override — picking "core default" back in the picker.

        The one way "no override" is represented: an absent row, never a stored sentinel
        (``model`` is a plain non-nullable string column). A no-op if there was none.
        """
        async with self._session() as session:
            await session.execute(
                delete(_StoredSessionModel).where(
                    _StoredSessionModel.tenant == tenant,
                    _StoredSessionModel.session_id == session_id,
                )
            )
            await session.commit()

    async def lookup(self, *, tenant: str, session_ids: list[str]) -> dict[str, str]:
        """Map each of *session_ids* that has a persisted override to it — the sessions
        list's batch enrichment read."""
        if not session_ids:
            return {}
        async with self._session() as session:
            rows = await session.scalars(
                select(_StoredSessionModel).where(
                    _StoredSessionModel.tenant == tenant,
                    _StoredSessionModel.session_id.in_(session_ids),
                )
            )
            return {row.session_id: row.model for row in rows}


__all__ = ["SessionModelStore"]
