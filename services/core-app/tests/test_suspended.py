"""Tests for the suspended-run store behind ask_user pause/resume (ADR-0053)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core_app.agent.suspended import SuspendedRunStore

TENANT = "test"


@pytest.fixture
async def store() -> SuspendedRunStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    s = SuspendedRunStore(engine)
    await s.init()
    return s


async def test_save_then_take_round_trips(store: SuspendedRunStore) -> None:
    convo = [
        {"role": "user", "content": "open the file"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
    ]
    run_id = await store.save(
        tenant=TENANT,
        session_id="s1",
        model="m",
        pending_call_id="c1",
        question="which file?",
        conversation=convo,
    )
    assert run_id
    run = await store.take(tenant=TENANT, run_id=run_id)
    assert run is not None
    assert run.session_id == "s1"
    assert run.model == "m"
    assert run.pending_call_id == "c1"
    assert run.question == "which file?"
    assert run.conversation == convo


async def test_take_consumes_the_run(store: SuspendedRunStore) -> None:
    run_id = await store.save(
        tenant=TENANT,
        session_id=None,
        model=None,
        pending_call_id="c1",
        question="q",
        conversation=[],
    )
    assert await store.take(tenant=TENANT, run_id=run_id) is not None
    # A second take finds nothing — resume is single-use, so a double-submit can't replay.
    assert await store.take(tenant=TENANT, run_id=run_id) is None


async def test_take_unknown_returns_none(store: SuspendedRunStore) -> None:
    assert await store.take(tenant=TENANT, run_id="does-not-exist") is None


async def test_tenant_isolation(store: SuspendedRunStore) -> None:
    run_id = await store.save(
        tenant="tenant-a",
        session_id=None,
        model=None,
        pending_call_id="c1",
        question="q",
        conversation=[],
    )
    assert await store.take(tenant="tenant-b", run_id=run_id) is None
    # still retrievable by the owning tenant
    assert await store.take(tenant="tenant-a", run_id=run_id) is not None
