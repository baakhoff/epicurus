"""In-flight agent turns, decoupled from the HTTP request that started them (#376).

A chat turn used to run *inline* in the SSE request generator, so a client disconnect
(a mobile PWA backgrounded, a hard refresh, a network blip) closed the generator and
unwound :meth:`Agent.run_stream` **before** the answer was persisted — the reply was
lost and the client was left stuck "streaming".

Here a turn instead runs in a **detached task** that consumes ``run_stream`` into an
ordered, seq-tagged in-memory buffer. HTTP subscribers replay that buffer then tail live
events until the run terminates; a subscriber disconnecting **never** cancels the task, so
the turn always runs to completion and :meth:`Agent._persist_answer` always writes the
answer to ``agent_messages``. A client that reconnects re-attaches to the live run (replay
from an offset) or, if it finished while away, simply reads the now-persisted transcript.

This buffer is a **disposable cache**, not authoritative state (constraint #2): the durable
answer is in Postgres, so on any miss — unknown/reaped run, or a different instance — the
client falls back to the conversation history. Multi-instance SaaS (a shared event log over
Valkey/NATS, or sticky routing by run id) is a deliberate follow-up; the registry interface
is the seam where that slots in.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from epicurus_core import get_logger
from epicurus_core_app.agent.agent import AgentEvent

log = get_logger("epicurus_core_app.agent.live_runs")

# Statuses that mean "the run produced everything it ever will". ``awaiting_input`` is
# terminal too: an ``ask_user`` pause ends the stream with no ``done`` (ADR-0053), and a
# re-attacher must replay it then stop rather than wait forever for a token that never comes.
_TERMINAL = frozenset({"done", "error", "awaiting_input"})

# A run's event source — ``lambda: agent.run_stream(...)``. Called once by the driver task.
RunStreamFactory = Callable[[], AsyncIterator[AgentEvent]]


class RunAlreadyActiveError(RuntimeError):
    """A turn is already running for this (tenant, session) — carries the active run id.

    The HTTP layer turns this into a 409 so a duplicate caller re-attaches instead of
    racing a second turn (two turns would race two ``_persist_answer`` writes).
    """

    def __init__(self, run_id: str) -> None:
        super().__init__(f"a run is already active for this session: {run_id}")
        self.run_id = run_id


@dataclass
class LiveRun:
    """One in-flight turn: an append-only event buffer any number of clients can tail.

    ``seq`` is 1-based and equals "index + 1", so ``after_seq=0`` replays from the very
    start (matching SSE ``Last-Event-ID`` semantics: "give me events after this id").

    Synchronisation is **lock-free** on purpose. Under cooperative scheduling only one coroutine
    runs between awaits, so the append-only list needs no lock — and, critically, marking a run
    terminal must be doable *synchronously*: the driver does it from inside an
    ``except CancelledError`` handler (a user ``stop`` / shutdown drain), where ``await``-ing
    would be re-cancelled before it ran and leave subscribers hung forever. Wake-ups go through
    one-shot futures, resolved synchronously by :meth:`_wake`.
    """

    run_id: str
    tenant: str
    session_id: str | None
    status: str = "running"
    finished_at: float | None = None
    _events: list[AgentEvent] = field(default_factory=list)
    _waiters: list[asyncio.Future[None]] = field(default_factory=list)
    _task: asyncio.Task[None] | None = field(default=None)

    @property
    def last_seq(self) -> int:
        """The seq of the most recent buffered event (0 when none yet)."""
        return len(self._events)

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL

    def _wake(self) -> None:
        """Resolve every pending subscriber future — synchronous, so it is safe to call while
        the driver task is being cancelled (no ``await`` to be re-cancelled)."""
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)

    def append(self, event: AgentEvent) -> None:
        """Buffer one event; mark terminal on a terminal type; wake subscribers."""
        self._events.append(event)
        if event.type in _TERMINAL and not self.terminal:
            self.status = event.type
            self.finished_at = time.monotonic()
        self._wake()

    def finish(self, detail: str) -> None:
        """Force a terminal ``error`` if the driver ended/crashed without one.

        Synchronous so it can run from an ``except CancelledError`` handler — guarding against a
        subscriber that would otherwise wait forever for a terminal frame that never arrives.
        """
        if not self.terminal:
            self._events.append(AgentEvent(type="error", detail=detail))
            self.status = "error"
            self.finished_at = time.monotonic()
        self._wake()

    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[tuple[int, AgentEvent]]:
        """Replay buffered events with ``seq > after_seq``, then tail live ones until terminal.

        Disconnecting — the consumer stops iterating / its task is cancelled — only unwinds this
        generator; the driver task is untouched, which is the whole point (the turn survives the
        client).
        """
        cursor = max(0, after_seq)
        while True:
            if cursor < len(self._events):
                start = cursor
                batch = self._events[cursor:]
                cursor = len(self._events)
                for offset, event in enumerate(batch):
                    yield start + offset + 1, event
                continue
            if self.terminal:
                return
            fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
            try:
                await fut
            finally:  # if cancelled before the next _wake, drop our waiter
                if fut in self._waiters:
                    self._waiters.remove(fut)


class LiveRunRegistry:
    """Owns in-flight runs: start (as a detached task), look up, re-attach, reap, drain."""

    def __init__(self, *, grace_seconds: float = 300.0) -> None:
        self._runs: dict[str, LiveRun] = {}
        # Index of the *current* run per (tenant, session) — only set for sessioned runs.
        self._by_session: dict[tuple[str, str], str] = {}
        self._grace = grace_seconds
        self._lock = asyncio.Lock()

    async def start(
        self,
        factory: RunStreamFactory,
        *,
        tenant: str,
        session_id: str | None,
    ) -> LiveRun:
        """Create a run and a detached task that drives ``factory()`` into its buffer.

        Enforces at most one *running* run per (tenant, session): a second start while one is
        live raises :class:`RunAlreadyActiveError`. Anonymous turns (``session_id is None``)
        skip the guard and the session index — they are ephemeral and not re-attachable by
        session (there is nothing durable to fall back to either).
        """
        async with self._lock:
            self._reap_locked()
            if session_id is not None:
                active = self._active_locked(tenant=tenant, session_id=session_id)
                if active is not None:
                    raise RunAlreadyActiveError(active.run_id)
            run_id = uuid.uuid4().hex
            run = LiveRun(run_id=run_id, tenant=tenant, session_id=session_id)
            self._runs[run_id] = run
            if session_id is not None:
                self._by_session[(tenant, session_id)] = run_id
            # create_task does not await, so the run is fully registered before any other
            # coroutine can observe it — no window where it exists without its driver.
            run._task = asyncio.create_task(self._drive(run, factory))
            return run

    async def _drive(self, run: LiveRun, factory: RunStreamFactory) -> None:
        """Consume the turn into the run's buffer; always leave it terminal."""
        try:
            async for event in factory():
                run.append(event)
            if not run.terminal:  # run_stream should always end terminal; be defensive
                run.finish("turn ended without a terminal event")
        except asyncio.CancelledError:
            # The driver was cancelled — either a user ``stop`` (see ``cancel``) or the
            # shutdown drain. A turn cancelled mid-generation is discarded (the shielded
            # ``_persist_answer`` only protects an *already-finished* answer); mark terminal
            # *synchronously* (no await — it would be re-cancelled) so subscribers unblock.
            run.finish("the turn was cancelled")
            raise
        except Exception as exc:  # never let the detached task die silently
            log.warning("live run driver failed", run_id=run.run_id, error=str(exc))
            run.finish(str(exc))

    def get(self, run_id: str, *, tenant: str) -> LiveRun | None:
        """The run by id, scoped to ``tenant`` (constraint #1); ``None`` if absent/foreign."""
        run = self._runs.get(run_id)
        return run if run is not None and run.tenant == tenant else None

    def active_for_session(self, *, tenant: str, session_id: str) -> LiveRun | None:
        """The session's *running* run (for re-attach discovery), or ``None`` if none is live.

        A terminal run (done/error/awaiting_input) returns ``None`` — the client then reads
        the durable transcript instead of re-attaching to a dead buffer.
        """
        return self._active_locked(tenant=tenant, session_id=session_id)

    def _active_locked(self, *, tenant: str, session_id: str) -> LiveRun | None:
        # Pure dict reads — safe without awaiting the lock (no interleave on one event loop).
        run_id = self._by_session.get((tenant, session_id))
        if run_id is None:
            return None
        run = self._runs.get(run_id)
        return run if run is not None and not run.terminal else None

    def active_sessions(self, *, tenant: str) -> list[str]:
        """Session ids with a live (non-terminal) run right now, for the conversations-list
        running indicator (#396).

        Tenant-scoped (constraint #1) and limited to *sessioned* runs (anonymous turns have no
        row to surface). Pure dict reads — safe without awaiting the lock (no interleave on one
        event loop). Point-in-time and best-effort: the buffer is a disposable cache
        (constraint #2), so a multi-instance deployment only sees this instance's runs.
        """
        return [
            session_id
            for (run_tenant, session_id), run_id in self._by_session.items()
            if run_tenant == tenant
            and (run := self._runs.get(run_id)) is not None
            and not run.terminal
        ]

    async def cancel(self, run: LiveRun) -> None:
        """Cancel a run's driver task — for an explicit user ``stop`` (#376).

        Because a turn is now decoupled from the request, a client disconnect no longer ends
        it; ``stop`` must say so explicitly, or the turn would keep running (and block the next
        send via the one-run guard). Mid-generation work is discarded.

        Marks the run terminal **directly** rather than relying on the driver's
        ``except CancelledError``: if the task hasn't taken its first step yet, cancelling it
        raises *before* its ``try`` runs, so the ``except`` would never fire and subscribers
        would hang. ``finish`` is a no-op once the run is already terminal.
        """
        task = run._task
        if task is not None and not task.done():
            task.cancel()
        run.finish("the turn was cancelled")

    def _reap_locked(self) -> None:
        """Evict terminal runs whose grace window has elapsed (the answer is durable anyway)."""
        now = time.monotonic()
        dead = [
            rid
            for rid, run in self._runs.items()
            if run.finished_at is not None and now - run.finished_at > self._grace
        ]
        for rid in dead:
            run = self._runs.pop(rid)
            if run.session_id is not None:
                key = (run.tenant, run.session_id)
                if self._by_session.get(key) == rid:
                    del self._by_session[key]

    async def reap_periodically(self, *, interval: float = 60.0) -> None:
        """Background sweep so finished runs are evicted even with no new ``start`` traffic."""
        while True:
            await asyncio.sleep(interval)
            async with self._lock:
                self._reap_locked()

    async def drain(self, *, timeout: float = 5.0) -> None:
        """Let in-flight turns finish, then cancel stragglers — for shutdown.

        Gives running turns a brief window to complete on their own (so they persist via the
        normal path), then cancels whatever is left (e.g. a wedged model call). Call before
        disposing the DB engine: a turn finishing in the window — and the shielded
        ``_persist_answer`` in run_stream — both need the engine alive to flush the answer.
        A turn cancelled mid-generation is lost from memory but recoverable via regenerate
        (the user message is already durable).
        """
        tasks = [run._task for run in self._runs.values() if run._task is not None]
        pending = [task for task in tasks if not task.done()]
        if not pending:
            return
        _, still_running = await asyncio.wait(pending, timeout=timeout)
        for task in still_running:
            task.cancel()
        for task in still_running:
            with contextlib.suppress(asyncio.CancelledError):
                await task
