"""The chat delete cascade (#771) — deleting a chat deletes everything it produced.

The regression this guards: ``DELETE /sessions/{id}`` used to remove only the message rows,
so the extraction queue (carrying the conversation *text*), the attachment bytes, the model
override, and every paused run survived a "successful" delete — and the nightly drain then
distilled the deleted conversation into memory facts. These tests build a session that
populates **every** store, delete it, and assert zero survivors per table — plus that the
drain afterwards provably learns nothing from it, and that another tenant's identically-named
session is untouched.

In-memory SQLite + StaticPool is fine here: every store call runs in the test's own task (the
live-run driver task in the cancel test touches no store), so the AGENTS.md concurrent-tasks
pitfall does not apply.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from epicurus_core_app.agent.agent import AgentEvent
from epicurus_core_app.agent.live_runs import LiveRunRegistry
from epicurus_core_app.agent.pending_approvals import PendingApprovalStore
from epicurus_core_app.agent.pending_drafts import PendingDraftStore
from epicurus_core_app.agent.session_delete import SessionDeleteCascade
from epicurus_core_app.agent.session_model import SessionModelStore
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.automations.store import AutomationSessionStore
from epicurus_core_app.memory.extraction import ExtractionRunner
from epicurus_core_app.memory.extraction_queue import ExtractionQueue
from epicurus_core_app.memory.store import AttachmentStore, ConversationStore


class _Stores:
    """Every store a conversation can touch, on one engine, plus the cascade over them."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.store = ConversationStore(engine)
        self.attachments = AttachmentStore(engine)
        self.queue = ExtractionQueue(engine)
        self.session_models = SessionModelStore(engine)
        self.suspended = SuspendedRunStore(engine)
        self.pending_drafts = PendingDraftStore(engine)
        self.pending_approvals = PendingApprovalStore(engine)
        self.automation_sessions = AutomationSessionStore(engine)
        self.live_runs = LiveRunRegistry()
        self.cascade = SessionDeleteCascade(
            store=self.store,
            attachments=self.attachments,
            queue=self.queue,
            session_models=self.session_models,
            suspended=self.suspended,
            pending_drafts=self.pending_drafts,
            pending_approvals=self.pending_approvals,
            automation_sessions=self.automation_sessions,
            live_runs=self.live_runs,
        )

    async def init(self) -> None:
        await self.store.init()  # shared Base: also creates the queue + attachments tables
        await self.queue.init()
        await self.session_models.init()
        await self.suspended.init()
        await self.pending_drafts.init()
        await self.pending_approvals.init()
        await self.automation_sessions.init()


@pytest.fixture
async def stores() -> AsyncIterator[_Stores]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    built = _Stores(engine)
    await built.init()
    yield built
    await engine.dispose()


async def _populate(
    stores: _Stores, *, tenant: str, session_id: str, pk_sidecars: bool = True
) -> dict[str, str]:
    """Fill every store for one session; returns the ids needed to assert erasure.

    ``pk_sidecars=False`` skips the two sidecars keyed by ``session_id`` as a global primary
    key (``session_models``, ``automation_sessions``): a cross-tenant *id collision* is
    unrepresentable there by design (session ids are client-minted uuids), so the same-id/
    other-tenant fixture leaves them out rather than having the second write steal the row.
    """
    att_id = await stores.attachments.save(
        tenant=tenant, kind="text/plain", title="notes.txt", content=b"attached bytes"
    )
    await stores.store.append(
        tenant=tenant,
        session_id=session_id,
        role="user",
        content=f"{session_id}: the question",
        attachments=[{"att_id": att_id, "source": "file", "kind": "text/plain", "title": "n"}],
    )
    await stores.store.append(
        tenant=tenant, session_id=session_id, role="assistant", content="the answer"
    )
    await stores.queue.enqueue(
        tenant=tenant,
        user_text=f"{session_id}: the question",
        assistant_text="the answer",
        session_id=session_id,
    )
    if pk_sidecars:
        await stores.session_models.set(tenant=tenant, session_id=session_id, model="llama3.2")
    suspended_id = await stores.suspended.save(
        tenant=tenant,
        session_id=session_id,
        model=None,
        pending_call_id="call-1",
        question="which one?",
        conversation=[{"role": "user", "content": "the question"}],
    )
    draft_id = await stores.pending_drafts.save(
        tenant=tenant,
        session_id=session_id,
        model=None,
        pending_call_id="call-2",
        tool="mail_send",
        module="mail",
        summary="Email to bob",
        draft={"to": ["bob@example.com"], "body": "hi"},
        conversation=[{"role": "user", "content": "the question"}],
    )
    approval_id = await stores.pending_approvals.save(
        tenant=tenant,
        session_id=session_id,
        model=None,
        pending_call_id="call-3",
        summary="Edit the doc",
        refs=[],
        conversation=[{"role": "user", "content": "the question"}],
    )
    if pk_sidecars:
        await stores.automation_sessions.record(
            tenant=tenant,
            session_id=session_id,
            automation_id="auto-1",
            name="Weekly report",
            chat_mode="rolling",
        )
    return {
        "att_id": att_id,
        "suspended": suspended_id,
        "draft": draft_id,
        "approval": approval_id,
    }


async def test_cascade_erases_every_store_and_only_this_session(stores: _Stores) -> None:
    mine = await _populate(stores, tenant="t1", session_id="s")
    other_session = await _populate(stores, tenant="t1", session_id="other")
    # The same session id under another tenant — representable in every tenant+session-scoped
    # store; the two id-primary-keyed sidecars are skipped for it (see _populate).
    other_tenant = await _populate(stores, tenant="t2", session_id="s", pk_sidecars=False)

    result = await stores.cascade.delete(tenant="t1", session_id="s")

    assert result.deleted == 2  # the user + assistant messages
    assert result.attachments == 1
    assert result.queued_extractions == 1
    assert result.session_model_cleared is True
    assert result.suspended_runs == 1
    assert result.pending_drafts == 1
    assert result.pending_approvals == 1
    assert result.live_run_cancelled is False  # nothing was streaming

    # Zero survivors, table by table.
    assert await stores.store.messages(tenant="t1", session_id="s") == []
    assert await stores.attachments.get(tenant="t1", att_id=mine["att_id"]) is None
    queued = await stores.queue.pending(limit=10)
    assert all(p.session_id != "s" or p.tenant != "t1" for p in queued)
    assert await stores.session_models.get(tenant="t1", session_id="s") is None
    assert await stores.suspended.take(tenant="t1", run_id=mine["suspended"]) is None
    assert await stores.pending_drafts.take(tenant="t1", run_id=mine["draft"]) is None
    assert await stores.pending_approvals.take(tenant="t1", run_id=mine["approval"]) is None
    assert await stores.automation_sessions.lookup(tenant="t1", session_ids=["s"]) == {}
    # The sessions list no longer shows it.
    assert [s.id for s in await stores.store.sessions(tenant="t1")] == ["other"]

    # The same tenant's other session and the other tenant's identically-named session are
    # byte-for-byte untouched.
    for tenant, session_id, ids in (("t1", "other", other_session), ("t2", "s", other_tenant)):
        assert len(await stores.store.messages(tenant=tenant, session_id=session_id)) == 2
        assert await stores.attachments.get(tenant=tenant, att_id=ids["att_id"]) is not None
        assert await stores.suspended.take(tenant=tenant, run_id=ids["suspended"]) is not None
        assert await stores.pending_drafts.take(tenant=tenant, run_id=ids["draft"]) is not None
        assert (
            await stores.pending_approvals.take(tenant=tenant, run_id=ids["approval"]) is not None
        )
    # The id-keyed sidecars: t1's other session kept its rows (only populated for pk_sidecars).
    assert await stores.session_models.get(tenant="t1", session_id="other") == "llama3.2"
    assert "other" in await stores.automation_sessions.lookup(tenant="t1", session_ids=["other"])
    assert await stores.queue.count() == 2  # the two surviving sessions' exchanges


async def test_cascade_is_idempotent_on_an_absent_session(stores: _Stores) -> None:
    result = await stores.cascade.delete(tenant="t1", session_id="never-existed")
    assert result.deleted == 0
    assert result.attachments == 0
    assert result.queued_extractions == 0
    assert result.session_model_cleared is False


async def test_drain_after_delete_extracts_nothing_from_the_deleted_session(
    stores: _Stores,
) -> None:
    """The worst survivor, closed: the nightly drain must not distil a deleted chat.

    Before #771 the queue rows had no ``session_id``, so a deleted chat's exchanges could not
    even be targeted — the drain distilled the "deleted" conversation into memory that night.
    """

    class _RecordingExtractor:
        def __init__(self) -> None:
            self.extracted: list[str] = []

        async def extract(self, *, tenant: str, user_text: str, assistant_text: str) -> list[str]:
            self.extracted.append(user_text)
            return []

    class _Power:
        paused = False

    await _populate(stores, tenant="t1", session_id="deleted")
    await _populate(stores, tenant="t1", session_id="kept")
    await stores.cascade.delete(tenant="t1", session_id="deleted")

    extractor = _RecordingExtractor()

    async def _tz() -> str:
        return "UTC"

    runner = ExtractionRunner(
        stores.queue,
        extractor,  # type: ignore[arg-type]
        _Power(),
        timezone=_tz,
    )
    processed = await runner.drain_once()
    assert processed == 1
    assert extractor.extracted == ["kept: the question"]  # nothing from the deleted session
    assert await stores.queue.count() == 0


async def test_cascade_cancels_and_evicts_the_live_run(stores: _Stores) -> None:
    """An in-flight turn must not outlive its conversation: the driver is cancelled (so it
    can't persist an answer into the erased session) and the buffer is evicted (so the
    deleted text stops being replayable), before any rows drop."""
    gate = asyncio.Event()

    async def held() -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="delta", text="partial")
        await gate.wait()
        yield AgentEvent(type="done", turn=None)

    await stores.store.append(tenant="t1", session_id="s", role="user", content="q")
    run = await stores.live_runs.start(held, tenant="t1", session_id="s")

    result = await stores.cascade.delete(tenant="t1", session_id="s")

    assert result.live_run_cancelled is True
    assert run.terminal  # subscribers unblock on the terminal frame rather than hanging
    assert stores.live_runs.get(run.run_id, tenant="t1") is None  # buffer evicted
    assert stores.live_runs.active_for_session(tenant="t1", session_id="s") is None
    assert await stores.store.messages(tenant="t1", session_id="s") == []
