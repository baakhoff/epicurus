"""Unit tests for the memory facade — the store and recall are faked (no DB/Qdrant)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.memory.recall import RecallHit, RecallPoint
from epicurus_core_app.memory.store import MessageMeta

_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeStore:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []  # tenant, session, role, content
        self._next_id = 0
        self.last_refs: list[dict[str, Any]] | None = None
        self.last_attachments: list[dict[str, Any]] | None = None
        self.last_activity: dict[str, Any] | None = None
        self.meta: dict[int, tuple[str, MessageMeta]] = {}  # id -> (tenant, meta)

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
        self.rows.append((tenant, session_id, role, content))
        self.last_refs = entity_refs
        self.last_attachments = attachments
        self.last_activity = activity
        self._next_id += 1
        self.meta[self._next_id] = (
            tenant,
            MessageMeta(role=role, created_at=_BASE_TIME + timedelta(seconds=self._next_id)),
        )
        return self._next_id

    async def history(self, *, tenant: str, session_id: str) -> list[tuple[str, str]]:
        return [(r, c) for (t, s, r, c) in self.rows if t == tenant and s == session_id]

    async def metadata_for(self, *, tenant: str, ids: list[int]) -> dict[int, MessageMeta]:
        return {i: meta for i, (t, meta) in self.meta.items() if t == tenant and i in ids}


class _FakeRecall:
    def __init__(self) -> None:
        self.indexed: list[tuple[str, str, int, str]] = []  # tenant, session, point_id, text

    async def index(self, *, tenant: str, session_id: str, text: str, point_id: int) -> None:
        self.indexed.append((tenant, session_id, point_id, text))

    def _for(self, tenant: str) -> list[tuple[str, str, int, str]]:
        return [row for row in self.indexed if row[0] == tenant]

    async def recall(self, *, tenant: str, query: str, limit: int = 4) -> list[str]:
        return [text for (t, _s, _pid, text) in self.indexed if t == tenant][:limit]

    async def count(self, *, tenant: str) -> int:
        return len(self._for(tenant))

    async def list_points(
        self, *, tenant: str, limit: int = 100, cap: int = 1000
    ) -> list[RecallPoint]:
        points = [
            RecallPoint(id=pid, session_id=s, text=text) for (_t, s, pid, text) in self._for(tenant)
        ]
        points.sort(key=lambda p: p.id, reverse=True)
        return points[:limit]

    async def search(self, *, tenant: str, query: str, limit: int = 20) -> list[RecallHit]:
        rows = self._for(tenant)[:limit]
        return [
            RecallHit(id=pid, session_id=s, text=text, score=1.0 - i * 0.1)
            for i, (_t, s, pid, text) in enumerate(rows)
        ]

    async def forget_point(self, *, tenant: str, point_id: int) -> int:
        before = len(self.indexed)
        self.indexed = [r for r in self.indexed if not (r[0] == tenant and r[2] == point_id)]
        return before - len(self.indexed)


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


async def test_remember_passes_entity_refs_to_the_store() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    refs = [{"ref_id": "e1", "module": "calendar", "kind": "event", "title": "Standup"}]
    await memory.remember(
        tenant="t", session_id="s", role="assistant", content="see standup", entity_refs=refs
    )
    assert store.last_refs == refs


async def test_remember_passes_attachments_to_the_store() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    atts = [{"att_id": "a1", "source": "file", "title": "notes.txt"}]
    await memory.remember(
        tenant="t", session_id="s", role="user", content="see notes", attachments=atts
    )
    assert store.last_attachments == atts


async def test_remember_passes_activity_to_the_store() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    activity = {"thinking": "weighed it", "steps": [{"tool": "echo", "status": "ok"}]}
    await memory.remember(
        tenant="t", session_id="s", role="assistant", content="done", activity=activity
    )
    assert store.last_activity == activity


async def test_memories_returns_corpus_newest_first_with_role_and_time() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    await memory.remember(tenant="t1", session_id="s1", role="user", content="hello")
    await memory.remember(tenant="t1", session_id="s1", role="assistant", content="hi there")
    items, total = await memory.memories(tenant="t1")
    assert total == 2
    assert [i.id for i in items] == [2, 1]  # newest first
    assert (items[0].role, items[0].text) == ("assistant", "hi there")
    assert items[0].created_at is not None
    assert all(i.score is None for i in items)  # corpus rows carry no match score


async def test_memories_is_tenant_scoped() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    await memory.remember(tenant="t1", session_id="s1", role="user", content="mine")
    await memory.remember(tenant="t2", session_id="s1", role="user", content="theirs")
    items, total = await memory.memories(tenant="t1")
    assert total == 1
    assert [i.text for i in items] == ["mine"]


async def test_search_memory_sets_score_and_enriches_from_store() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    await memory.remember(tenant="t1", session_id="s1", role="user", content="alpha")
    items, total = await memory.search_memory(tenant="t1", query="alpha")
    assert total == 1
    assert items[0].text == "alpha"
    assert items[0].role == "user"
    assert items[0].score is not None


async def test_memories_degrade_gracefully_when_store_row_is_gone() -> None:
    # A recall vector whose source message no longer exists → no role/time, still listed.
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    await recall.index(tenant="t1", session_id="s1", text="orphan", point_id=99)
    items, total = await memory.memories(tenant="t1")
    assert total == 1
    assert items[0].text == "orphan"
    assert items[0].role == ""
    assert items[0].created_at is None


async def test_forget_memory_drops_the_snippet_from_recall() -> None:
    store, recall = _FakeStore(), _FakeRecall()
    memory = Memory(store, recall)
    await memory.remember(tenant="t1", session_id="s1", role="user", content="x")
    assert await memory.forget_memory(tenant="t1", point_id=1) == 1
    items, total = await memory.memories(tenant="t1")
    assert total == 0
    assert items == []
