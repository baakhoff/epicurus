"""Tests for the pending-draft store behind draft-first send Confirm/Decline (ADR-0085, #563)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core_app.agent.pending_drafts import PendingDraftStore

TENANT = "test"

_DRAFT = {"to": "bob@example.com", "subject": "Hi", "body": "Hello"}


@pytest.fixture
async def store() -> PendingDraftStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    s = PendingDraftStore(engine)
    await s.init()
    return s


async def _save(store: PendingDraftStore, *, tenant: str = TENANT) -> str:
    convo = [
        {"role": "user", "content": "email bob"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
    ]
    return await store.save(
        tenant=tenant,
        session_id="s1",
        model="m",
        pending_call_id="c1",
        tool="mail_send",
        module="mail",
        summary="Email to bob@example.com — Hi",
        draft=_DRAFT,
        conversation=convo,
    )


async def test_save_then_take_round_trips(store: PendingDraftStore) -> None:
    run_id = await _save(store)
    assert run_id
    run = await store.take(tenant=TENANT, run_id=run_id)
    assert run is not None
    assert run.session_id == "s1"
    assert run.model == "m"
    assert run.pending_call_id == "c1"
    assert run.tool == "mail_send"
    assert run.module == "mail"
    assert run.summary == "Email to bob@example.com — Hi"
    assert run.draft == _DRAFT
    assert any(m.get("role") == "assistant" for m in run.conversation)


async def test_take_consumes_the_draft(store: PendingDraftStore) -> None:
    run_id = await _save(store)
    assert await store.take(tenant=TENANT, run_id=run_id) is not None
    # A second take finds nothing — Confirm/Decline is single-use, so a double-submit can't
    # send twice or replay the turn.
    assert await store.take(tenant=TENANT, run_id=run_id) is None


async def test_take_unknown_returns_none(store: PendingDraftStore) -> None:
    assert await store.take(tenant=TENANT, run_id="does-not-exist") is None


async def test_tenant_isolation(store: PendingDraftStore) -> None:
    run_id = await _save(store, tenant="tenant-a")
    assert await store.take(tenant="tenant-b", run_id=run_id) is None
    # Still retrievable by the owning tenant (constraint #1).
    assert await store.take(tenant="tenant-a", run_id=run_id) is not None
