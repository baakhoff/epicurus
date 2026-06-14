"""Attachment expansion + the upload route (ADR-0019).

The expander runs against a real SQLite-backed AttachmentStore; memory and the module
registry are faked. The upload route is exercised end-to-end over ASGI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core import Attachment
from epicurus_core_app.agent.agent import Agent
from epicurus_core_app.agent.attachments import AttachmentExpander
from epicurus_core_app.agent.routes import create_agent_router
from epicurus_core_app.memory.memory import Memory
from epicurus_core_app.memory.store import AttachmentStore, ConversationStore, MessageRecord


async def _attachment_store() -> AttachmentStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    await ConversationStore(engine).init()  # creates the shared tables (incl. agent_attachments)
    return AttachmentStore(engine)


class _FakeMemory:
    def __init__(self, messages: list[MessageRecord]) -> None:
        self._messages = messages

    async def messages(self, *, tenant: str, session_id: str) -> list[MessageRecord]:
        return self._messages


class _FakeRegistry:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    async def resolve_attachment(self, name: str, ref_id: str) -> dict[str, Any]:
        return self._data


def _expander(
    store: AttachmentStore,
    *,
    memory: Any = None,
    registry: Any = None,
) -> AttachmentExpander:
    return AttachmentExpander(
        store=store,
        memory=cast(Memory, memory or _FakeMemory([])),
        registry=cast(Any, registry or _FakeRegistry({})),
    )


async def test_expand_file_attachment_includes_its_text() -> None:
    store = await _attachment_store()
    att_id = await store.save(tenant="t", kind="text/plain", title="notes.txt", content=b"buy milk")
    out = await _expander(store).expand(
        [Attachment(att_id=att_id, source="file", title="notes.txt")], tenant="t"
    )
    assert "buy milk" in out
    assert "notes.txt" in out


async def test_expand_chat_attachment_includes_the_transcript() -> None:
    store = await _attachment_store()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    history = [
        MessageRecord(role="user", content="hello", created_at=when),
        MessageRecord(role="assistant", content="hi there", created_at=when),
    ]
    out = await _expander(store, memory=_FakeMemory(history)).expand(
        [Attachment(att_id="x", source="chat", ref_id="s1", title="earlier chat")], tenant="t"
    )
    assert "hello" in out
    assert "hi there" in out


async def test_expand_module_attachment_uses_the_resolver_excerpt() -> None:
    store = await _attachment_store()
    out = await _expander(store, registry=_FakeRegistry({"excerpt": "milk, eggs"})).expand(
        [Attachment(att_id="x", source="module", module="notes", ref_id="n1", title="Groceries")],
        tenant="t",
    )
    assert "milk, eggs" in out


async def test_expand_skips_a_failing_attachment() -> None:
    store = await _attachment_store()

    class _BoomRegistry:
        async def resolve_attachment(self, name: str, ref_id: str) -> dict[str, Any]:
            raise RuntimeError("module down")

    out = await _expander(store, registry=_BoomRegistry()).expand(
        [Attachment(att_id="x", source="module", module="notes", ref_id="n1", title="X")],
        tenant="t",
    )
    assert out == ""


async def test_expand_missing_file_is_empty() -> None:
    store = await _attachment_store()
    out = await _expander(store).expand(
        [Attachment(att_id="gone", source="file", title="gone")], tenant="t"
    )
    assert out == ""


async def test_upload_route_stores_the_file_and_returns_a_handle() -> None:
    store = await _attachment_store()
    app = FastAPI()
    app.include_router(create_agent_router(cast(Agent, None), cast(Memory, None), "local", store))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/platform/v1/agent/attachments",
            files={"file": ("notes.txt", b"buy milk", "text/plain")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "notes.txt"
    row = await store.get(tenant="local", att_id=body["att_id"])
    assert row is not None
    assert row.content == b"buy milk"
