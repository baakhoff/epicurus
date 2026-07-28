"""Tests for the pending-approval store behind ``ask_approval`` (#745, ADR-0117)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from epicurus_core_app.agent.pending_approvals import PendingApprovalStore

TENANT = "test"

_REFS = [
    {
        "ref_id": "sugg-1",
        "module": "knowledge",
        "kind": "suggestion",
        "title": "Update goals.md",
    }
]


@pytest.fixture
async def store() -> PendingApprovalStore:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    s = PendingApprovalStore(engine)
    await s.init()
    return s


async def _save(
    store: PendingApprovalStore, *, tenant: str = TENANT, refs: list[dict[str, object]] = _REFS
) -> str:
    convo = [
        {"role": "user", "content": "update my goals note"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
    ]
    return await store.save(
        tenant=tenant,
        session_id="s1",
        model="m",
        pending_call_id="c1",
        summary="Update goals.md to add a Q3 milestone",
        refs=refs,
        conversation=convo,
    )


async def test_save_then_take_round_trips(store: PendingApprovalStore) -> None:
    run_id = await _save(store)
    assert run_id
    run = await store.take(tenant=TENANT, run_id=run_id)
    assert run is not None
    assert run.session_id == "s1"
    assert run.model == "m"
    assert run.pending_call_id == "c1"
    assert run.summary == "Update goals.md to add a Q3 milestone"
    assert run.refs == _REFS
    assert any(m.get("role") == "assistant" for m in run.conversation)


async def test_save_with_no_refs_round_trips_empty_list(store: PendingApprovalStore) -> None:
    """A propose tool that doesn't yet return a structured ref still works (summary-only)."""
    run_id = await _save(store, refs=[])
    run = await store.take(tenant=TENANT, run_id=run_id)
    assert run is not None
    assert run.refs == []


async def test_take_consumes_the_approval(store: PendingApprovalStore) -> None:
    run_id = await _save(store)
    assert await store.take(tenant=TENANT, run_id=run_id) is not None
    # A second take finds nothing — Approve/Reject is single-use, so a double-submit can't
    # resume the same turn twice.
    assert await store.take(tenant=TENANT, run_id=run_id) is None


async def test_take_unknown_returns_none(store: PendingApprovalStore) -> None:
    assert await store.take(tenant=TENANT, run_id="does-not-exist") is None


async def test_tenant_isolation(store: PendingApprovalStore) -> None:
    run_id = await _save(store, tenant="tenant-a")
    assert await store.take(tenant="tenant-b", run_id=run_id) is None
    # Still retrievable by the owning tenant (constraint #1).
    assert await store.take(tenant="tenant-a", run_id=run_id) is not None
