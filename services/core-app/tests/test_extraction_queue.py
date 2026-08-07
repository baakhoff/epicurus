"""Unit tests for the durable fact-extraction queue (ADR-0051).

Runs against an in-memory SQLite (the queries are portable, standard SQL); the production
queue targets Postgres. A StaticPool keeps the single in-memory connection alive across calls.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.memory.extraction_queue import ExtractionQueue
from epicurus_core_app.memory.store import ConversationStore


async def _fresh_queue() -> ExtractionQueue:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    queue = ExtractionQueue(engine)
    await queue.init()
    return queue


async def test_enqueue_then_pending_is_fifo() -> None:
    queue = await _fresh_queue()
    await queue.enqueue(tenant="t1", user_text="first", assistant_text="a1")
    await queue.enqueue(tenant="t1", user_text="second", assistant_text="a2")
    pending = await queue.pending(limit=10)
    assert [(p.user_text, p.assistant_text) for p in pending] == [
        ("first", "a1"),
        ("second", "a2"),
    ]


async def test_pending_respects_the_limit() -> None:
    queue = await _fresh_queue()
    for i in range(5):
        await queue.enqueue(tenant="t", user_text=f"u{i}", assistant_text="a")
    assert len(await queue.pending(limit=3)) == 3


async def test_enqueue_skips_blank_user_text() -> None:
    queue = await _fresh_queue()
    # Nothing durable to learn from a blank turn — drop it rather than queue an empty extraction.
    assert await queue.enqueue(tenant="t", user_text="   ", assistant_text="a") is None
    assert await queue.count() == 0


async def test_delete_removes_processed_rows() -> None:
    queue = await _fresh_queue()
    first = await queue.enqueue(tenant="t", user_text="u1", assistant_text="a")
    assert first is not None
    await queue.enqueue(tenant="t", user_text="u2", assistant_text="a")
    assert await queue.delete([first]) == 1
    remaining = await queue.pending(limit=10)
    assert [p.user_text for p in remaining] == ["u2"]


async def test_delete_empty_is_a_noop() -> None:
    queue = await _fresh_queue()
    assert await queue.delete([]) == 0


async def test_count_and_pending_are_tenant_scoped() -> None:
    queue = await _fresh_queue()
    await queue.enqueue(tenant="t1", user_text="t1 only", assistant_text="a")
    await queue.enqueue(tenant="t2", user_text="t2 only", assistant_text="a")
    assert await queue.count(tenant="t1") == 1
    assert await queue.count() == 2  # every tenant
    t1 = await queue.pending(limit=10, tenant="t1")
    assert [p.user_text for p in t1] == ["t1 only"]


async def test_enqueue_stamps_the_session_and_pending_reads_it_back() -> None:
    """#771: each exchange carries the conversation it came from, so a deleted chat can purge
    its still-queued rows. A caller with no session (a headless bridge turn) stamps nothing."""
    queue = await _fresh_queue()
    await queue.enqueue(tenant="t", user_text="in a chat", assistant_text="a", session_id="s1")
    await queue.enqueue(tenant="t", user_text="no session", assistant_text="a")
    pending = await queue.pending(limit=10)
    assert [(p.user_text, p.session_id) for p in pending] == [
        ("in a chat", "s1"),
        ("no session", None),
    ]


async def test_delete_for_session_purges_only_that_chat_and_tenant() -> None:
    queue = await _fresh_queue()
    await queue.enqueue(tenant="t1", user_text="goes", assistant_text="a", session_id="s")
    await queue.enqueue(tenant="t1", user_text="stays", assistant_text="a", session_id="other")
    await queue.enqueue(tenant="t2", user_text="other tenant", assistant_text="a", session_id="s")
    await queue.enqueue(tenant="t1", user_text="legacy", assistant_text="a")  # no stamp

    assert await queue.delete_for_session(tenant="t1", session_id="s") == 1
    remaining = [p.user_text for p in await queue.pending(limit=10)]
    # The other session, the other tenant's identical id, and the unstamped legacy row survive.
    assert remaining == ["stays", "other tenant", "legacy"]


async def test_init_adds_session_id_to_a_legacy_queue_table() -> None:
    """A pre-#771 deployment: memory_extraction_queue exists without the session_id column.

    ``init`` must reconcile it in place (ADR-0067) so both the stamped enqueue and the cascade's
    purge work after an upgrade; the legacy rows stay NULL and drain exactly as before.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE memory_extraction_queue ("
            "id INTEGER PRIMARY KEY, tenant VARCHAR(63), user_text TEXT, assistant_text TEXT, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO memory_extraction_queue (tenant, user_text, assistant_text) "
            "VALUES ('t', 'legacy row', 'a')"
        )
    queue = ExtractionQueue(engine)
    await queue.init()  # must add session_id in place, not raise
    await queue.enqueue(tenant="t", user_text="new row", assistant_text="a", session_id="s")
    pending = await queue.pending(limit=10)
    assert [(p.user_text, p.session_id) for p in pending] == [
        ("legacy row", None),
        ("new row", "s"),
    ]
    # The legacy NULL row is untargetable by session — exactly the drain-as-today behaviour.
    assert await queue.delete_for_session(tenant="t", session_id="s") == 1
    assert [p.user_text for p in await queue.pending(limit=10)] == ["legacy row"]


async def test_a_queued_exchange_outlives_the_messages_it_came_from() -> None:
    """Editing mid-history truncates turns that may still be queued for extraction (#552).

    A queued row must survive that cleanly, and it does *by construction*: the queue copies the
    exchange's text at enqueue time rather than referencing ``agent_messages``, so a truncated
    turn leaves no dangling id for the nightly runner to resolve — there is nothing to skip.
    The facts such an exchange yields are kept deliberately: a fact belongs to the user, not to
    the turn that surfaced it (the same rule as ``Memory.forget``).
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = ConversationStore(engine)  # shares Base, so this creates the queue table too
    await store.init()
    queue = ExtractionQueue(engine)

    anchor = await store.append(tenant="t1", session_id="s", role="user", content="first ask")
    await store.append(tenant="t1", session_id="s", role="assistant", content="first answer")
    later = await store.append(tenant="t1", session_id="s", role="user", content="later ask")
    await store.append(tenant="t1", session_id="s", role="assistant", content="later answer")
    await queue.enqueue(tenant="t1", user_text="later ask", assistant_text="later answer")

    # The user edits the first message: everything after it — including the queued turn — goes.
    removed = await store.truncate_after(tenant="t1", session_id="s", after_id=anchor)
    assert later in removed

    pending = await queue.pending(limit=10)
    assert [(p.user_text, p.assistant_text) for p in pending] == [("later ask", "later answer")]
    assert await queue.delete([p.id for p in pending]) == 1  # drains without touching messages
    assert await queue.count() == 0
