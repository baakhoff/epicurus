"""Unit tests for LlmPrefsStore — tenant-scoped hidden list and global default.

Uses an in-memory SQLite database with StaticPool (same pattern as test_memory_store.py).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.llm.prefs import LlmPrefsStore


async def _fresh_store() -> tuple[LlmPrefsStore, AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    store = LlmPrefsStore(engine)
    await store.init()
    return store, engine


async def test_hidden_defaults_to_empty() -> None:
    store, _ = await _fresh_store()
    assert await store.get_hidden("t1") == []


async def test_global_default_defaults_to_none() -> None:
    store, _ = await _fresh_store()
    assert await store.get_default("t1") is None


async def test_set_and_get_hidden() -> None:
    store, _ = await _fresh_store()
    await store.set_hidden("t1", ["phi3:mini", "llama3.2:1b"])
    assert await store.get_hidden("t1") == ["phi3:mini", "llama3.2:1b"]


async def test_hidden_is_tenant_scoped() -> None:
    store, _ = await _fresh_store()
    await store.set_hidden("t1", ["model-a"])
    assert await store.get_hidden("t2") == []


async def test_set_and_get_default() -> None:
    store, _ = await _fresh_store()
    await store.set_default("t1", "qwen2.5:7b")
    assert await store.get_default("t1") == "qwen2.5:7b"


async def test_default_is_tenant_scoped() -> None:
    store, _ = await _fresh_store()
    await store.set_default("t1", "qwen2.5:7b")
    assert await store.get_default("t2") is None


async def test_clear_default() -> None:
    store, _ = await _fresh_store()
    await store.set_default("t1", "qwen2.5:7b")
    await store.set_default("t1", None)
    assert await store.get_default("t1") is None


async def test_replace_hidden_list() -> None:
    store, _ = await _fresh_store()
    await store.set_hidden("t1", ["a", "b"])
    await store.set_hidden("t1", ["c"])
    assert await store.get_hidden("t1") == ["c"]


async def test_multiple_updates_without_duplicate_rows() -> None:
    store, _ = await _fresh_store()
    # two set_default calls must update the same row, not create two
    await store.set_default("t1", "model-x")
    await store.set_default("t1", "model-y")
    assert await store.get_default("t1") == "model-y"


# ── Global embedding default ───────────────────────────────────────────────────


async def test_embed_default_defaults_to_none() -> None:
    store, _ = await _fresh_store()
    assert await store.get_embed_default("t1") is None


async def test_set_and_get_embed_default() -> None:
    store, _ = await _fresh_store()
    await store.set_embed_default("t1", "nomic-embed-text")
    assert await store.get_embed_default("t1") == "nomic-embed-text"


async def test_embed_default_is_tenant_scoped() -> None:
    store, _ = await _fresh_store()
    await store.set_embed_default("t1", "nomic-embed-text")
    assert await store.get_embed_default("t2") is None


async def test_clear_embed_default() -> None:
    store, _ = await _fresh_store()
    await store.set_embed_default("t1", "nomic-embed-text")
    await store.set_embed_default("t1", None)
    assert await store.get_embed_default("t1") is None


async def test_embed_default_and_chat_default_coexist() -> None:
    store, _ = await _fresh_store()
    await store.set_default("t1", "qwen2.5:7b")
    await store.set_embed_default("t1", "nomic-embed-text")
    assert await store.get_default("t1") == "qwen2.5:7b"
    assert await store.get_embed_default("t1") == "nomic-embed-text"
