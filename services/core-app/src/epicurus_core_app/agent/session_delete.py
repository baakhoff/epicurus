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
from epicurus_core_app.memory.store import AttachmentStore, ConversationStore, EphemeralSessionStore

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
    # The invisible-chat flag row (#772), dropped as the cascade's final step.
    ephemeral_flag_cleared: bool = False


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
        ephemeral: EphemeralSessionStore | None = None,
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
        self._ephemeral = ephemeral

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
        # 5. The attachment bytes, then the messages themselves, then — truly last — the
        #    invisible-chat flag row (#772): while any of the above could still fail, the flag
        #    must survive so the orphan sweep retries the whole erase.
        attachments = await self._attachments.delete_many(tenant=tenant, att_ids=att_ids)
        messages = await self._store.delete_session(tenant=tenant, session_id=session_id)
        flag_cleared = False
        if self._ephemeral is not None:
            flag_cleared = (await self._ephemeral.clear(tenant=tenant, session_id=session_id)) > 0
        result = DeletedSession(
            deleted=messages,
            attachments=attachments,
            queued_extractions=queued,
            session_model_cleared=model_cleared,
            suspended_runs=suspended,
            pending_drafts=drafts,
            pending_approvals=approvals,
            live_run_cancelled=cancelled,
            ephemeral_flag_cleared=flag_cleared,
        )
        log.info(
            "session deleted",
            tenant=tenant,
            session_id=session_id,
            **result.model_dump(exclude={"deleted"}),
            messages=messages,
        )
        return result

    async def sweep_ephemeral(self, *, tenant: str, keep: str | None = None) -> int:
        """Delete every stranded invisible chat (#772); returns how many were erased.

        The safety net behind the exit paths: the client deletes an invisible chat when the
        operator leaves it, but a crash (or an app close that never got its request out) can
        strand one on disk — flagged, hidden, and never cleaned. Runs at startup (nothing can
        be live across a restart) and on session-list reads, where ``keep`` is the session the
        requesting client says it is currently *in* (`GET /sessions?active=…`), spared so
        opening the sheet mid-invisible-chat can't nuke the live conversation. A session with
        a **non-terminal live run** is spared too, regardless of ``keep`` — another client's
        turn in flight is proof the chat isn't stranded. Best-effort per session: one failed
        erase is logged and left flagged for the next sweep, never aborting the rest.
        """
        if self._ephemeral is None:
            return 0
        swept = 0
        for session_id in await self._ephemeral.list_ids(tenant=tenant):
            if session_id == keep:
                continue
            if (
                self._live_runs is not None
                and self._live_runs.active_for_session(tenant=tenant, session_id=session_id)
                is not None
            ):
                continue
            try:
                await self.delete(tenant=tenant, session_id=session_id)
                swept += 1
            except Exception as exc:  # leave it flagged; the next sweep retries
                log.warning(
                    "ephemeral sweep failed for a session",
                    tenant=tenant,
                    session_id=session_id,
                    error=str(exc),
                )
        if swept:
            log.info("swept stranded invisible chats", tenant=tenant, count=swept)
        return swept
