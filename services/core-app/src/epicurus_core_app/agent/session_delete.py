"""Deleting a chat deletes the chat — everything it produced, everywhere (#771).

``DELETE /agent/v1/sessions/{id}`` used to remove only the ``agent_messages`` rows, so a
"deleted" conversation lived on in every sidecar the session had touched: the fact-extraction
queue still carried the exchange *text* (and the nightly drain distilled the deleted chat into
memory facts that night), the attachment bytes, the per-session model override, and any
suspended/pending paused runs all survived, and an in-flight turn kept generating into the
deleted session. This module is the one tenant-scoped operation that removes all of it.

**Scope is the session's own artifacts, deliberately.** Facts extracted on *previous* nights
are curated memory, managed in the Memory view — deleting a chat stops all future derivation
from it but does not un-learn what was already distilled (the confirm dialog says so). Tool
effects (a task created, a mail sent) belong to the modules that own them. A ``scheduled_turns``
row *delivering into* the session is operator-authored standing config, kept — its next run
simply starts the conversation afresh. ``notifications`` rows carry no session linkage and are
an independent activity record, kept. The ``automation_sessions`` badge mapping for the session
is cleared — it describes rows that no longer exist, and a rolling automation's next run
re-records it (the store's ``record`` is an upsert).

**Ordering matters.** The live run is cancelled first (so nothing writes into the session while
it is being erased), the attachment ids are collected from the messages *before* those messages
drop (the link lives only in their JSON), and the messages themselves go **last** — so a failure
partway leaves the session visible in the list and the whole operation retriable (every step is
idempotent).
"""

from __future__ import annotations

from pydantic import BaseModel

from epicurus_core import get_logger
from epicurus_core_app.agent.live_runs import LiveRunRegistry
from epicurus_core_app.agent.pending_approvals import PendingApprovalStore
from epicurus_core_app.agent.pending_drafts import PendingDraftStore
from epicurus_core_app.agent.session_model import SessionModelStore
from epicurus_core_app.agent.suspended import SuspendedRunStore
from epicurus_core_app.automations.store import AutomationSessionStore
from epicurus_core_app.memory.extraction_queue import ExtractionQueue
from epicurus_core_app.memory.store import AttachmentStore, ConversationStore

log = get_logger("epicurus_core_app.agent.session_delete")


class DeletedSession(BaseModel):
    """What one delete removed, per store — the route's response body.

    ``deleted`` is the message count (the field the pre-#771 response already carried, kept
    first for compatibility); the rest are additive detail (ADR-0055).
    """

    deleted: int
    attachments: int = 0
    queued_extractions: int = 0
    session_model_cleared: bool = False
    suspended_runs: int = 0
    pending_drafts: int = 0
    pending_approvals: int = 0
    live_run_cancelled: bool = False


class SessionDeleteCascade:
    """Erase one conversation across every store that holds a piece of it (#771).

    Constructed once in ``app.py`` with the same store instances the agent runs on. Every step
    is tenant-scoped (constraint #1) and idempotent; an exception propagates (the route answers
    5xx) with the messages still present, so the operator retries rather than being told a
    partial delete succeeded.
    """

    def __init__(
        self,
        *,
        store: ConversationStore,
        attachments: AttachmentStore,
        queue: ExtractionQueue | None = None,
        session_models: SessionModelStore | None = None,
        suspended: SuspendedRunStore | None = None,
        pending_drafts: PendingDraftStore | None = None,
        pending_approvals: PendingApprovalStore | None = None,
        automation_sessions: AutomationSessionStore | None = None,
        live_runs: LiveRunRegistry | None = None,
    ) -> None:
        self._store = store
        self._attachments = attachments
        self._queue = queue
        self._session_models = session_models
        self._suspended = suspended
        self._pending_drafts = pending_drafts
        self._pending_approvals = pending_approvals
        self._automation_sessions = automation_sessions
        self._live_runs = live_runs

    async def delete(self, *, tenant: str, session_id: str) -> DeletedSession:
        """Run the cascade; returns per-store counts. See the module docstring for ordering."""
        # 1. Stop and evict any in-flight turn first: a live driver would otherwise keep
        #    generating and persist an answer into the session being erased.
        cancelled = False
        if self._live_runs is not None:
            cancelled = await self._live_runs.discard_session(tenant=tenant, session_id=session_id)
        # 2. Collect the attachment ids while the messages (the only place the link lives)
        #    still exist.
        att_ids = await self._store.attachment_ids(tenant=tenant, session_id=session_id)
        # 3. Purge the still-queued extractions — the worst survivor: rows carrying the
        #    conversation text that the nightly drain would otherwise distil into memory.
        queued = 0
        if self._queue is not None:
            queued = await self._queue.delete_for_session(tenant=tenant, session_id=session_id)
        # 4. The sidecars: model override, paused runs, the automation badge mapping.
        model_cleared = False
        if self._session_models is not None:
            model_cleared = (
                await self._session_models.get(tenant=tenant, session_id=session_id) is not None
            )
            await self._session_models.clear(tenant=tenant, session_id=session_id)
        suspended = 0
        if self._suspended is not None:
            suspended = await self._suspended.delete_for_session(
                tenant=tenant, session_id=session_id
            )
        drafts = 0
        if self._pending_drafts is not None:
            drafts = await self._pending_drafts.delete_for_session(
                tenant=tenant, session_id=session_id
            )
        approvals = 0
        if self._pending_approvals is not None:
            approvals = await self._pending_approvals.delete_for_session(
                tenant=tenant, session_id=session_id
            )
        if self._automation_sessions is not None:
            # The badge mapping describes rows about to be erased; a rolling automation's next
            # run re-records it (the store's ``record`` is an upsert) into the same session id.
            await self._automation_sessions.delete_session(tenant=tenant, session_id=session_id)
        # 5. The attachment bytes, then — last — the messages themselves.
        attachments = await self._attachments.delete_many(tenant=tenant, att_ids=att_ids)
        messages = await self._store.delete_session(tenant=tenant, session_id=session_id)
        result = DeletedSession(
            deleted=messages,
            attachments=attachments,
            queued_extractions=queued,
            session_model_cleared=model_cleared,
            suspended_runs=suspended,
            pending_drafts=drafts,
            pending_approvals=approvals,
            live_run_cancelled=cancelled,
        )
        log.info(
            "session deleted",
            tenant=tenant,
            session_id=session_id,
            **result.model_dump(exclude={"deleted"}),
            messages=messages,
        )
        return result
