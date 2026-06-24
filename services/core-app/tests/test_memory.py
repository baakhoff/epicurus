"""Unit tests for the memory facade — the store and fact store are faked (no DB/Qdrant)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from epicurus_core_app.memory.facts import SOURCE_AUTO, SOURCE_TOOL, UserFact, UserFactHit
from epicurus_core_app.memory.memory import Memory

_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeStore:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []  # tenant, session, role, content
        self._next_id = 0
        self.last_refs: list[dict[str, Any]] | None = None
        self.last_attachments: list[dict[str, Any]] | None = None
        self.last_activity: dict[str, Any] | None = None

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
        return self._next_id

    async def history(self, *, tenant: str, session_id: str) -> list[tuple[str, str]]:
        return [(r, c) for (t, s, r, c) in self.rows if t == tenant and s == session_id]

    async def delete_session(self, *, tenant: str, session_id: str) -> int:
        before = len(self.rows)
        self.rows = [row for row in self.rows if not (row[0] == tenant and row[1] == session_id)]
        return before - len(self.rows)


class _FakeFacts:
    """A faithful in-memory stand-in for UserFactStore (tenant-scoped, newest-first)."""

    def __init__(self) -> None:
        self._rows: list[tuple[str, UserFact]] = []  # (tenant, fact)
        self._clock = 0

    def _for(self, tenant: str) -> list[UserFact]:
        facts = [f for (t, f) in self._rows if t == tenant]
        facts.sort(key=lambda f: f.created_at or _BASE_TIME, reverse=True)
        return facts

    async def save(self, *, tenant: str, text: str, source: str = SOURCE_AUTO) -> UserFact | None:
        text = text.strip()
        if not text:
            return None
        if any(f.text == text for f in self._for(tenant)):  # simple dedup by exact text
            return None
        self._clock += 1
        fact = UserFact(
            id=str(uuid.uuid4()),
            text=text,
            source=source,
            created_at=_BASE_TIME + timedelta(seconds=self._clock),
        )
        self._rows.append((tenant, fact))
        return fact

    async def recall(self, *, tenant: str, query: str, limit: int = 8) -> list[str]:
        return [f.text for f in self._for(tenant)][:limit]

    async def search(self, *, tenant: str, query: str, limit: int = 20) -> list[UserFactHit]:
        return [
            UserFactHit(**f.model_dump(), score=1.0 - i * 0.1)
            for i, f in enumerate(self._for(tenant)[:limit])
        ]

    async def list_facts(self, *, tenant: str, limit: int = 200) -> list[UserFact]:
        return self._for(tenant)[:limit]

    async def count(self, *, tenant: str) -> int:
        return len(self._for(tenant))

    async def forget(self, *, tenant: str, fact_id: str) -> int:
        before = len(self._rows)
        self._rows = [(t, f) for (t, f) in self._rows if not (t == tenant and f.id == fact_id)]
        return before - len(self._rows)


def _memory() -> tuple[Memory, _FakeStore, _FakeFacts]:
    store, facts = _FakeStore(), _FakeFacts()
    return Memory(store, facts), store, facts  # type: ignore[arg-type]


# ── transcript persistence (no longer indexed for recall) ─────────────────────


async def test_remember_persists_messages_but_does_not_index_them() -> None:
    memory, store, facts = _memory()
    await memory.remember(tenant="t1", session_id="s1", role="user", content="hello")
    await memory.remember(tenant="t1", session_id="s1", role="assistant", content="hi there")
    assert [(r, c) for (_t, _s, r, c) in store.rows] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]
    # messages are NOT dumped into the recall corpus — that corpus is facts (ADR-0045)
    assert await facts.count(tenant="t1") == 0


async def test_remember_skips_empty_content() -> None:
    memory, store, _ = _memory()
    await memory.remember(tenant="t1", session_id="s1", role="assistant", content="")
    assert store.rows == []


async def test_history_returns_chat_messages_in_order() -> None:
    memory, _, _ = _memory()
    await memory.remember(tenant="t1", session_id="s1", role="user", content="one")
    await memory.remember(tenant="t1", session_id="s1", role="assistant", content="two")
    history = await memory.history(tenant="t1", session_id="s1")
    assert [(m.role, m.content) for m in history] == [("user", "one"), ("assistant", "two")]


async def test_remember_passes_entity_refs_attachments_activity_to_the_store() -> None:
    memory, store, _ = _memory()
    refs = [{"ref_id": "e1", "module": "calendar", "kind": "event", "title": "Standup"}]
    atts = [{"att_id": "a1", "source": "file", "title": "notes.txt"}]
    activity = {"thinking": "weighed it", "steps": [{"tool": "echo", "status": "ok"}]}
    await memory.remember(
        tenant="t",
        session_id="s",
        role="assistant",
        content="done",
        entity_refs=refs,
        attachments=atts,
        activity=activity,
    )
    assert store.last_refs == refs
    assert store.last_attachments == atts
    assert store.last_activity == activity


# ── user-fact memory (the recall corpus) ──────────────────────────────────────


async def test_remember_fact_saves_and_recall_returns_facts() -> None:
    memory, _, _ = _memory()
    await memory.remember_fact(tenant="t1", text="Prefers metric units", source=SOURCE_TOOL)
    assert await memory.recall(tenant="t1", query="units") == ["Prefers metric units"]


async def test_recall_is_tenant_scoped() -> None:
    memory, _, _ = _memory()
    await memory.remember_fact(tenant="t1", text="alpha")
    await memory.remember_fact(tenant="t2", text="beta")
    assert await memory.recall(tenant="t1", query="x") == ["alpha"]
    assert await memory.recall(tenant="t2", query="x") == ["beta"]


async def test_memories_returns_corpus_newest_first_with_source() -> None:
    memory, _, _ = _memory()
    await memory.remember_fact(tenant="t1", text="Lives in Belgrade", source=SOURCE_AUTO)
    await memory.remember_fact(tenant="t1", text="Prefers metric units", source=SOURCE_TOOL)
    items, total = await memory.memories(tenant="t1")
    assert total == 2
    assert items[0].text == "Prefers metric units"  # newest first
    assert items[0].source == SOURCE_TOOL
    assert items[0].created_at is not None
    assert all(i.score is None for i in items)  # corpus rows carry no match score


async def test_memories_is_tenant_scoped() -> None:
    memory, _, _ = _memory()
    await memory.remember_fact(tenant="t1", text="mine")
    await memory.remember_fact(tenant="t2", text="theirs")
    items, total = await memory.memories(tenant="t1")
    assert total == 1
    assert [i.text for i in items] == ["mine"]


async def test_search_memory_sets_score() -> None:
    memory, _, _ = _memory()
    await memory.remember_fact(tenant="t1", text="alpha")
    items, total = await memory.search_memory(tenant="t1", query="alpha")
    assert total == 1
    assert items[0].text == "alpha"
    assert items[0].score is not None


async def test_forget_memory_drops_one_fact() -> None:
    memory, _, _ = _memory()
    saved = await memory.remember_fact(tenant="t1", text="x")
    assert saved is not None
    assert await memory.forget_memory(tenant="t1", memory_id=saved.id) == 1
    items, total = await memory.memories(tenant="t1")
    assert total == 0
    assert items == []


async def test_forget_session_keeps_facts() -> None:
    # Deleting a conversation erases its transcript but leaves the user's facts intact.
    memory, _, facts = _memory()
    await memory.remember(tenant="t1", session_id="s1", role="user", content="my name is Sam")
    await memory.remember_fact(tenant="t1", text="Name is Sam", source=SOURCE_AUTO)
    removed = await memory.forget(tenant="t1", session_id="s1")
    assert removed == 1
    assert (await memory.history(tenant="t1", session_id="s1")) == []
    assert await facts.count(tenant="t1") == 1  # the fact survives the conversation
