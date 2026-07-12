"""Unit tests for the conversation store's SQL: tenant isolation, titles, ordering.

Runs against an in-memory SQLite (the queries are portable, standard SQL); the
production store targets Postgres. A StaticPool keeps the single in-memory
connection alive across the store's operations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.memory.store import AttachmentStore, ConversationStore, StoredMessage


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


async def test_search_messages_is_tenant_scoped_and_case_insensitive() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t1", session_id="s", role="user", content="Our Backup Strategy")
    await store.append(tenant="t2", session_id="s", role="user", content="backup strategy leaks?")

    hits = await store.search_messages(tenant="t1", query="backup")
    assert [h.content for h in hits] == ["Our Backup Strategy"]  # matched despite the case
    assert hits[0].session_id == "s"
    assert hits[0].role == "user"
    # the other tenant's identical topic never surfaces
    assert [h.content for h in await store.search_messages(tenant="t2", query="leaks")] == [
        "backup strategy leaks?"
    ]


async def test_search_messages_orders_newest_first_and_caps() -> None:
    store, engine = await _fresh_store()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                StoredMessage(
                    tenant="t",
                    session_id="s",
                    role="user",
                    content=f"note about topic {i}",
                    created_at=base + timedelta(hours=i),
                )
                for i in range(4)
            ]
        )
        await session.commit()
    hits = await store.search_messages(tenant="t", query="topic", limit=2)
    # newest first, capped at the limit
    assert [h.content for h in hits] == ["note about topic 3", "note about topic 2"]


async def test_search_messages_treats_like_metacharacters_literally() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t", session_id="s", role="user", content="save 50% today")
    await store.append(tenant="t", session_id="s", role="user", content="nothing on sale")
    # Without escaping, '50%' would match via the trailing wildcard; with escaping it's literal.
    assert [h.content for h in await store.search_messages(tenant="t", query="50%")] == [
        "save 50% today"
    ]
    assert await store.search_messages(tenant="t", query="zz%") == []


async def test_search_messages_blank_query_matches_nothing() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t", session_id="s", role="user", content="anything")
    assert await store.search_messages(tenant="t", query="   ") == []


async def test_session_titles_maps_first_message_per_session() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t", session_id="s1", role="user", content="First of s1")
    await store.append(tenant="t", session_id="s1", role="assistant", content="reply")
    await store.append(tenant="t", session_id="s2", role="user", content="First of s2")
    titles = await store.session_titles(tenant="t", session_ids=["s1", "s2", "missing"])
    assert titles == {"s1": "First of s1", "s2": "First of s2"}
    assert await store.session_titles(tenant="t", session_ids=[]) == {}


async def test_entity_refs_persist_and_round_trip() -> None:
    store, _ = await _fresh_store()
    refs = [{"ref_id": "e1", "module": "calendar", "kind": "event", "title": "Standup"}]
    await store.append(
        tenant="t", session_id="s", role="assistant", content="see your standup", entity_refs=refs
    )
    messages = await store.messages(tenant="t", session_id="s")
    assert messages[0].entity_refs[0].ref_id == "e1"
    assert messages[0].entity_refs[0].title == "Standup"


async def test_messages_without_entity_refs_default_to_empty() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t", session_id="s", role="user", content="hi")
    record = (await store.messages(tenant="t", session_id="s"))[0]
    assert record.entity_refs == []
    assert record.attachments == []


async def test_attachments_persist_and_round_trip() -> None:
    store, _ = await _fresh_store()
    atts = [{"att_id": "a1", "source": "file", "kind": "text/plain", "title": "notes.txt"}]
    await store.append(
        tenant="t", session_id="s", role="user", content="see notes", attachments=atts
    )
    record = (await store.messages(tenant="t", session_id="s"))[0]
    assert record.attachments[0].att_id == "a1"
    assert record.attachments[0].source == "file"


async def test_activity_persists_and_round_trips() -> None:
    store, _ = await _fresh_store()
    activity = {
        "thinking": "weighed it",
        "steps": [{"tool": "knowledge_search", "status": "ok", "detail": '{"q": "x"}'}],
    }
    await store.append(
        tenant="t", session_id="s", role="assistant", content="here", activity=activity
    )
    record = (await store.messages(tenant="t", session_id="s"))[0]
    assert record.activity is not None
    assert record.activity.thinking == "weighed it"
    assert record.activity.steps[0].tool == "knowledge_search"
    assert record.activity.steps[0].status == "ok"
    assert record.activity.steps[0].detail == '{"q": "x"}'


async def test_messages_without_activity_default_to_none() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t", session_id="s", role="assistant", content="plain")
    record = (await store.messages(tenant="t", session_id="s"))[0]
    assert record.activity is None


async def test_attachment_store_save_and_get_is_tenant_scoped() -> None:
    _, engine = await _fresh_store()
    blobs = AttachmentStore(engine)
    att_id = await blobs.save(tenant="t1", kind="text/plain", title="notes.txt", content=b"hello")
    row = await blobs.get(tenant="t1", att_id=att_id)
    assert row is not None
    assert row.content == b"hello"
    assert row.title == "notes.txt"
    assert await blobs.get(tenant="t2", att_id=att_id) is None  # other tenant can't read it
    assert await blobs.get(tenant="t1", att_id="missing") is None


async def test_init_adds_entity_refs_column_to_a_legacy_table() -> None:
    # A pre-v0.3 deployment: agent_messages exists without the entity_refs column.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE agent_messages ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63), session_id VARCHAR(128), "
            "role VARCHAR(16), content TEXT, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
    store = ConversationStore(engine)
    await store.init()  # must add entity_refs + attachments + activity columns in place, not raise
    await store.append(
        tenant="t",
        session_id="s",
        role="assistant",
        content="hi",
        entity_refs=[{"ref_id": "e1", "module": "m", "kind": "k", "title": "T"}],
        attachments=[{"att_id": "a1", "source": "file", "title": "n"}],
        activity={"thinking": "hmm", "steps": []},
    )
    record = (await store.messages(tenant="t", session_id="s"))[0]
    assert record.entity_refs[0].ref_id == "e1"
    assert record.attachments[0].att_id == "a1"
    assert record.activity is not None and record.activity.thinking == "hmm"


# ── regenerate / edit support: last id, in-place update, truncate (#302) ─────


async def test_last_message_id_with_and_without_role() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t1", session_id="s", role="user", content="q1")
    await store.append(tenant="t1", session_id="s", role="assistant", content="a1")
    u2 = await store.append(tenant="t1", session_id="s", role="user", content="q2")
    a2 = await store.append(tenant="t1", session_id="s", role="assistant", content="a2")
    assert await store.last_message_id(tenant="t1", session_id="s") == a2
    assert await store.last_message_id(tenant="t1", session_id="s", role="user") == u2
    assert await store.last_message_id(tenant="t1", session_id="empty") is None


async def test_update_content_replaces_in_place() -> None:
    store, _ = await _fresh_store()
    mid = await store.append(tenant="t1", session_id="s", role="user", content="oops")
    await store.update_content(tenant="t1", message_id=mid, content="fixed")
    assert [m.content for m in await store.messages(tenant="t1", session_id="s")] == ["fixed"]


async def test_truncate_after_drops_the_tail_and_returns_ids() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t1", session_id="s", role="user", content="q")
    await store.append(tenant="t1", session_id="s", role="assistant", content="a")
    u2 = await store.append(tenant="t1", session_id="s", role="user", content="q2")
    a2 = await store.append(tenant="t1", session_id="s", role="assistant", content="a2")
    removed = await store.truncate_after(tenant="t1", session_id="s", after_id=u2)
    assert removed == [a2]
    kept = [m.content for m in await store.messages(tenant="t1", session_id="s")]
    assert kept == ["q", "a", "q2"]


async def test_truncate_after_is_tenant_and_session_scoped() -> None:
    store, _ = await _fresh_store()
    a = await store.append(tenant="t1", session_id="s", role="user", content="x")
    await store.append(tenant="t2", session_id="s", role="user", content="y")  # other tenant
    await store.append(tenant="t1", session_id="other", role="user", content="z")  # other session
    removed = await store.truncate_after(tenant="t1", session_id="s", after_id=a - 1)
    assert removed == [a]  # only this tenant's, this session's tail
    assert [m.content for m in await store.messages(tenant="t2", session_id="s")] == ["y"]
    assert [m.content for m in await store.messages(tenant="t1", session_id="other")] == ["z"]


async def test_distinct_tenants_lists_every_tenant_with_history() -> None:
    store, _ = await _fresh_store()
    await store.append(tenant="t1", session_id="a", role="user", content="hi")
    await store.append(tenant="t1", session_id="b", role="user", content="again")
    await store.append(tenant="t2", session_id="a", role="user", content="hi")
    assert sorted(await store.distinct_tenants()) == ["t1", "t2"]  # deduped across sessions


async def test_distinct_tenants_is_empty_with_no_history() -> None:
    store, _ = await _fresh_store()
    assert await store.distinct_tenants() == []
