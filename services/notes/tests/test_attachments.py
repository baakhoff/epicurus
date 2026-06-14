"""Tests for the chat-attachment surface (#134): picker + resolve."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_notes.attachments import NotesAttachments, create_attachments_router
from epicurus_notes.db import NotesStore

TENANT = "test"


async def _attachments() -> tuple[NotesAttachments, NotesStore]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = NotesStore(engine)
    await store.init()
    return NotesAttachments(store, tenant=TENANT), store


async def test_list_items_returns_notes_as_attachables() -> None:
    attach, store = await _attachments()
    await store.upsert(tenant=TENANT, slug="a", title="Alpha", content="x")
    items = await attach.list_items()
    assert items[0].ref_id == "a"
    assert items[0].kind == "note"
    assert items[0].title == "Alpha"


async def test_resolve_returns_full_body() -> None:
    attach, store = await _attachments()
    await store.upsert(tenant=TENANT, slug="a", title="Alpha", content="the whole note body")
    resolved = await attach.resolve("a")
    assert resolved.title == "Alpha"
    assert resolved.excerpt == "the whole note body"


async def test_resolve_missing_is_404() -> None:
    attach, _ = await _attachments()
    with pytest.raises(HTTPException) as err:
        await attach.resolve("ghost")
    assert err.value.status_code == 404


async def test_router_lists_and_resolves() -> None:
    attach, store = await _attachments()
    await store.upsert(tenant=TENANT, slug="note-1", title="One", content="body one")
    app = FastAPI()
    app.include_router(create_attachments_router(attach))
    client = TestClient(app)

    listed = client.get("/attachments")
    assert listed.status_code == 200
    assert listed.json()[0] == {"ref_id": "note-1", "kind": "note", "title": "One"}

    resolved = client.get("/attachments/note-1")
    assert resolved.status_code == 200
    assert resolved.json()["excerpt"] == "body one"
