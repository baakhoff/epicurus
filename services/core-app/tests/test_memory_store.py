"""Unit tests for the conversation store's SQL: tenant isolation, titles, ordering.

Runs against an in-memory SQLite (the queries are portable, standard SQL); the
production store targets Postgres. A StaticPool keeps the single in-memory
connection alive across the store's operations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.memory.store import ConversationStore, StoredMessage


async def _fresh_store() -> tuple[ConversationStore, AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = ConversationStore(engine)
    await store.init()
    return store, engine


async def test_sessions_and_messages_are_tenant_scoped() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t1", session_id="s", role="user", content="t1 only")
    await store.append(tenant="t2", session_id="s", role="user", content="t2 only")

    t1 = await store.sessions(tenant="t1")
    assert [s.id for s in t1] == ["s"]
    assert t1[0].title == "t1 only"
    assert t1[0].message_count == 1
    # a session read never crosses the tenant boundary
    assert [m.content for m in await store.messages(tenant="t1", session_id="s")] == ["t1 only"]


async def test_session_title_is_the_first_message() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t", session_id="s", role="user", content="opening question")
    await store.append(tenant="t", session_id="s", role="assistant", content="the answer")
    summary = (await store.sessions(tenant="t"))[0]
    assert summary.title == "opening question"
    assert summary.message_count == 2


async def test_sessions_ordered_by_recent_activity() -> None:
    store, engine = await _fresh_store()
    # Explicit timestamps make the ordering deterministic (server-now would tie).
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                StoredMessage(
                    tenant="t", session_id="older", role="user", content="x", created_at=base
                ),
                StoredMessage(
                    tenant="t",
                    session_id="newer",
                    role="user",
                    content="y",
                    created_at=base + timedelta(hours=3),
                ),
            ]
        )
        await session.commit()
    assert [s.id for s in await store.sessions(tenant="t")] == ["newer", "older"]


async def test_delete_session_is_tenant_scoped() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t1", session_id="s", role="user", content="goes away")
    await store.append(tenant="t2", session_id="s", role="user", content="stays")

    removed = await store.delete_session(tenant="t1", session_id="s")
    assert removed == 1
    assert await store.sessions(tenant="t1") == []
    assert [s.id for s in await store.sessions(tenant="t2")] == ["s"]  # other tenant untouched
