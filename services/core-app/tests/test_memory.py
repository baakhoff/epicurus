"""Unit tests for the memory facade — the store and recall are faked (no DB/Qdrant)."""

from __future__ import annotations

from epicurus_core_app.memory.memory import Memory


class _FakeStore:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []  # tenant, session, role, content
        self._next_id = 0

    async def append(self, *, tenant: str, session_id: str, role: str, content: str) -> int:
        self.rows.append((tenant, session_id, role, content))
        self._next_id += 1
        return self._next_id

    async def history(self, *, tenant: str, session_id: str) -> list[tuple[str, str]]:
        return [(r, c) for (t, s, r, c) in self.rows if t == tenant and s == session_id]


class _FakeRecall:
    def __init__(self) -> None:
        self.indexed: list[tuple[str, str, int, str]] = []  # tenant, session, point_id, text

    async def index(self, *, tenant: str, session_id: str, text: str, point_id: int) -> None:
        self.indexed.append((tenant, session_id, point_id, text))

    async def recall(self, *, tenant: str, query: str, limit: int = 4) -> list[str]:
        return [text for (t, _s, _pid, text) in self.indexed if t == tenant][:limit]


async def test_remember_persists_and_indexes_user_and_assistant() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    await memory.remember(tenant="t1", session_id="s1", role="user", content="hello")
    await memory.remember(tenant="t1", session_id="s1", role="assistant", content="hi there")
    assert [(r, c) for (_t, _s, r, c) in store.rows] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]
    # both turns indexed for recall, keyed by the id the store assigned
    indexed = [(pid, text) for (_t, _s, pid, text) in recall.indexed]
    assert indexed == [(1, "hello"), (2, "hi there")]


async def test_remember_skips_empty_content() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    await memory.remember(tenant="t1", session_id="s1", role="assistant", content="")
    assert store.rows == []
    assert recall.indexed == []


async def test_history_returns_chat_messages_in_order() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    await memory.remember(tenant="t1", session_id="s1", role="user", content="one")
    await memory.remember(tenant="t1", session_id="s1", role="assistant", content="two")
    history = await memory.history(tenant="t1", session_id="s1")
    assert [(m.role, m.content) for m in history] == [("user", "one"), ("assistant", "two")]


async def test_history_is_tenant_scoped() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    await memory.remember(tenant="t1", session_id="s1", role="user", content="tenant one")
    await memory.remember(tenant="t2", session_id="s1", role="user", content="tenant two")
    assert [m.content for m in await memory.history(tenant="t1", session_id="s1")] == ["tenant one"]
    assert [m.content for m in await memory.history(tenant="t2", session_id="s1")] == ["tenant two"]


async def test_recall_is_tenant_scoped() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    await memory.remember(tenant="t1", session_id="s1", role="user", content="alpha")
    await memory.remember(tenant="t2", session_id="s1", role="user", content="beta")
    assert await memory.recall(tenant="t1", query="x") == ["alpha"]
    assert await memory.recall(tenant="t2", query="x") == ["beta"]
