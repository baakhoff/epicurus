"""Sessions: one agent process each, an inbox, a turn loop, and the permission relay.

Ordering rule: every event is made durable (and given its sequence number) before it is
fanned out, and — where Hermarchus itself performs the side effect — before the side effect:
``input.delivered`` before the prompt is sent, ``permission.resolved`` before the agent gets
its answer, ``file.write`` before the bytes are written, ``session.stopping`` /
``session.interrupted`` before the process is signalled. Where the agent performs the effect
(``agent.*`` updates: a tool call it ran, text it produced) Hermarchus only learns of it
afterwards, so those are recorded after the fact; a crash can lose the tail of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from epicurus_hermarchus.acp import (
    INVALID_PARAMS,
    RESOURCE_NOT_FOUND,
    AcpAgent,
    AgentExited,
    AgentStartError,
    RpcError,
)
from epicurus_hermarchus.broker import Broker
from epicurus_hermarchus.kinds import AgentKind
from epicurus_hermarchus.metrics import Metrics
from epicurus_hermarchus.sandbox import PathError, build_child_env, confine_path, resolve_workdir
from epicurus_hermarchus.settings import HermarchusSettings
from epicurus_hermarchus.store import Event, Store

__all__ = [
    "DECISIONS",
    "INPUT_MODES",
    "TERMINAL_EVENTS",
    "ConflictError",
    "LimitError",
    "LiveSession",
    "NotFoundError",
    "SessionManager",
    "UnavailableError",
]

log = structlog.get_logger("hermarchus.runtime")

INPUT_MODES = ("queue", "steer")
DECISIONS = ("once", "always", "reject")
# After one of these a session's log is complete; a following stream ends.
TERMINAL_EVENTS = ("session.stopped", "session.failed", "session.interrupted")

_UPDATE_EVENTS = {
    "agent_message_chunk": "agent.message",
    "agent_thought_chunk": "agent.thought",
    "user_message_chunk": "agent.user_message",
    "tool_call": "agent.tool_call",
    "tool_call_update": "agent.tool_call_update",
    "plan": "agent.plan",
}


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    """The request is valid but not in the session's current state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LimitError(RuntimeError):
    pass


class UnavailableError(RuntimeError):
    pass


def _pick_option(options: list[dict[str, Any]], kinds: tuple[str, ...]) -> str | None:
    for wanted in kinds:
        for option in options:
            if option.get("kind") == wanted and isinstance(option.get("optionId"), str):
                return str(option["optionId"])
    return None


def option_for(decision: str, options: list[dict[str, Any]]) -> str | None:
    """The agent's option id that carries out a controller decision, if it offered one.

    Never more than was decided: ``once`` does not fall back to ``allow_always`` (an agent
    program may remember that choice itself, for good); ``always`` may fall back to
    ``allow_once``, which grants less.
    """
    if decision == "always":
        return _pick_option(options, ("allow_always", "allow_once"))
    if decision == "once":
        return _pick_option(options, ("allow_once",))
    if decision == "reject":
        return _pick_option(options, ("reject_once", "reject_always"))
    return None


@dataclass
class _Waiter:
    future: asyncio.Future[str]
    tool_call: dict[str, Any]
    options: list[dict[str, Any]]


class LiveSession:
    """A session with an agent process. Implements :class:`~.acp.AgentHost`."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        session_id: str,
        kind: AgentKind,
        workdir: Path,
        cwd_rel: str,
        unattended: bool,
    ) -> None:
        self.manager = manager
        self.id = session_id
        self.kind = kind
        self.workdir = workdir
        self.cwd_rel = cwd_rel
        self.unattended = unattended
        self.adapter: AcpAgent | None = None
        self.turn_running = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._exit_watch: asyncio.Task[int] | None = None
        self._waiters: dict[str, _Waiter] = {}
        self._ending = False

    @property
    def store(self) -> Store:
        return self.manager.store

    async def record(self, type_: str, data: dict[str, Any] | None = None) -> Event:
        return await self.manager.record(self.id, type_, data)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def launch(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"session-{self.id}")

    async def _run(self) -> None:
        settings = self.manager.settings
        env = build_child_env(
            agent_home=settings.agent_home,
            parent_env=os.environ,
            passthrough=settings.passthrough_names,
            kind_env=self.kind.env,
            secrets=[settings.bearer_token()],
        )
        self.adapter = AcpAgent(
            argv=self.kind.argv(),
            cwd=self.workdir,
            env=env,
            host=self,
            start_timeout_s=settings.agent_start_timeout_s,
        )
        try:
            info = await self.adapter.start()
        except AgentStartError as exc:
            await self._fail(str(exc))
            return
        await self.store.update_session(
            self.id, status="idle", agent_session_id=info.agent_session_id
        )
        await self.record(
            "session.ready",
            {
                "protocol_version": info.protocol_version,
                "agent_info": info.agent_info,
                "agent_session_id": info.agent_session_id,
            },
        )
        self._exit_watch = asyncio.create_task(self.adapter.wait())
        while True:
            pending = await self.store.pending_inputs(self.id)
            if not pending:
                self._wake.clear()
                waiter = asyncio.create_task(self._wake.wait())
                done, _ = await asyncio.wait(
                    {waiter, self._exit_watch}, return_when=asyncio.FIRST_COMPLETED
                )
                if self._exit_watch in done:
                    waiter.cancel()
                    await self._fail(
                        f"the agent process exited (code {self.adapter.returncode}) between turns"
                    )
                    return
                continue
            steer = [p for p in pending if p["mode"] == "steer"]
            item = steer[0] if steer else pending[0]
            if not await self._turn(item):
                return

    async def _turn(self, item: dict[str, Any]) -> bool:
        """Deliver one input as one turn; ``False`` when the session has ended."""
        assert self.adapter is not None and self._exit_watch is not None
        await self.store.mark_input(item["id"], "delivered")
        await self.record(
            "input.delivered", {"input_id": item["id"], "mode": item["mode"], "text": item["text"]}
        )
        self.turn_running = True
        await self.store.update_session(self.id, status="running")
        await self.record("turn.started", {"input_id": item["id"]})
        prompt = asyncio.create_task(self.adapter.prompt(item["text"]))
        try:
            done, _ = await asyncio.wait(
                {prompt, self._exit_watch}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # Being stopped mid-turn: drop the prompt synchronously (no await in here).
            prompt.cancel()
            raise
        if prompt not in done:
            prompt.cancel()
            self.turn_running = False
            await self._fail(
                f"the agent process exited (code {self.adapter.returncode}) during a turn"
            )
            return False
        self.turn_running = False
        try:
            stop_reason = prompt.result()
        except RpcError as exc:
            await self.record(
                "turn.ended",
                {"input_id": item["id"], "stop_reason": "error", "error": exc.message},
            )
        except AgentExited:
            await self._fail(f"the agent process exited (code {self.adapter.returncode})")
            return False
        else:
            await self.record("turn.ended", {"input_id": item["id"], "stop_reason": stop_reason})
        await self.store.update_session(self.id, status="idle")
        return True

    async def _fail(self, reason: str) -> None:
        """The agent is gone without being asked to stop: record why, release everything."""
        if self._ending:
            return
        self._ending = True
        tail = self.adapter.stderr_tail() if self.adapter else []
        code = self.adapter.returncode if self.adapter else None
        await self._release_pending("session_failed")
        await self.store.update_session(self.id, status="failed", detail=reason)
        # No longer live from here on: it no longer counts against the limit, and the
        # terminal event below is what ends a following stream.
        self.manager.forget(self.id)
        await self.record("session.failed", {"reason": reason, "exit_code": code, "stderr": tail})
        if self.adapter is not None:
            await self.adapter.close(self.manager.settings.stop_grace_s)

    async def _release_pending(self, by: str) -> None:
        await self._cancel_permissions(by)
        for input_id in await self.store.drop_pending_inputs(self.id):
            await self.record("input.dropped", {"input_id": input_id, "reason": by})

    async def end(self, *, status: str, event: str, reason: str) -> None:
        """Stop the agent on purpose (stop, or host shutdown). Idempotent."""
        if self._ending:
            return
        self._ending = True
        # The event first: a crash during the teardown still leaves the intent on record.
        await self.record("session.stopping", {"reason": reason})
        if self.adapter is not None and self.turn_running:
            await self.adapter.cancel()
        await self._cancel_permissions(reason)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        code = None
        if self.adapter is not None:
            code = await self.adapter.close(self.manager.settings.stop_grace_s)
        if self._exit_watch is not None:
            self._exit_watch.cancel()
        for input_id in await self.store.drop_pending_inputs(self.id):
            await self.record("input.dropped", {"input_id": input_id, "reason": reason})
        self.turn_running = False
        await self.store.update_session(self.id, status=status, detail=reason)
        await self.record(event, {"reason": reason, "exit_code": code})
        self.manager.forget(self.id)

    # ── input ───────────────────────────────────────────────────────────────
    def wake(self) -> None:
        self._wake.set()

    async def cancel_turn(self, reason: str, input_id: str | None = None) -> None:
        if not self.turn_running or self.adapter is None:
            raise ConflictError("no_turn_running", "no turn is running in this session")
        data: dict[str, Any] = {"reason": reason}
        if input_id:
            data["input_id"] = input_id
        await self.record("turn.cancel_requested", data)
        # ACP: once a turn is cancelled, outstanding permission requests are answered
        # `cancelled` by the client.
        await self._cancel_permissions("cancel")
        await self.adapter.cancel()

    # ── permissions ─────────────────────────────────────────────────────────
    def _timeout(self) -> float:
        s = self.manager.settings
        return s.unattended_timeout_s if self.unattended else s.permission_timeout_s

    async def request_permission(
        self, tool_call: dict[str, Any], options: list[dict[str, Any]]
    ) -> dict[str, Any]:
        tool_kind = str(tool_call.get("kind") or "other")
        title = str(tool_call.get("title") or "")
        rule = {
            "project_dir": self.cwd_rel,
            "agent": self.kind.kind,
            "tool_kind": tool_kind,
            "title": title,
        }
        if await self.store.is_remembered(**rule):
            option = option_for("always", options)
            if option is not None:
                await self.record(
                    "permission.resolved",
                    {
                        "request_id": None,
                        "decision": "always",
                        "by": "remembered",
                        "option_id": option,
                        "tool_call": tool_call,
                    },
                )
                self.manager.metrics.permission_resolutions.labels("always", "remembered").inc()
                return {"outcome": "selected", "optionId": option}
        timeout = self._timeout()
        row = await self.store.create_permission(
            self.id, {"tool_call": tool_call, "options": options}, timeout
        )
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiters[row["id"]] = _Waiter(future, tool_call, options)
        try:
            await self.record(
                "permission.requested",
                {
                    "request_id": row["id"],
                    "tool_call": tool_call,
                    "options": options,
                    "unattended": self.unattended,
                    "expires_at": row["expires_at"],
                },
            )
            try:
                decision = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except TimeoutError:
                decision = await self._expire(row["id"], future)
        finally:
            self._waiters.pop(row["id"], None)
        if decision == "cancelled":
            return {"outcome": "cancelled"}
        option = option_for(decision, options)
        if option is None:
            # The agent offered no option that carries the decision out (no reject option,
            # say): `cancelled` is the protocol's only other answer, and it never allows.
            return {"outcome": "cancelled"}
        return {"outcome": "selected", "optionId": option}

    async def _expire(self, request_id: str, future: asyncio.Future[str]) -> str:
        if await self.store.resolve_permission(request_id, decision="reject", by="timeout"):
            await self.record(
                "permission.resolved",
                {
                    "request_id": request_id,
                    "decision": "reject",
                    "by": "timeout",
                    "option_id": None,
                },
            )
            self.manager.metrics.permission_resolutions.labels("reject", "timeout").inc()
            return "reject"
        # Lost the race to an answer that is being recorded right now: take that answer.
        return await future

    async def answer(self, request_id: str, decision: str) -> Event:
        waiter = self._waiters.get(request_id)
        if waiter is None or waiter.future.done():
            raise ConflictError(
                "permission_not_pending", "this permission request is no longer pending"
            )
        if not await self.store.resolve_permission(request_id, decision=decision, by="controller"):
            raise ConflictError(
                "permission_not_pending", "this permission request is no longer pending"
            )
        option = option_for(decision, waiter.options)
        if decision == "always" and option is not None:
            await self.store.remember(
                project_dir=self.cwd_rel,
                agent=self.kind.kind,
                tool_kind=str(waiter.tool_call.get("kind") or "other"),
                title=str(waiter.tool_call.get("title") or ""),
            )
        event = await self.record(
            "permission.resolved",
            {
                "request_id": request_id,
                "decision": decision,
                "by": "controller",
                "option_id": option,
                "remembered": decision == "always" and option is not None,
            },
        )
        self.manager.metrics.permission_resolutions.labels(decision, "controller").inc()
        if not waiter.future.done():
            waiter.future.set_result(decision)
        return event

    async def _cancel_permissions(self, by: str) -> None:
        for request_id, waiter in list(self._waiters.items()):
            if waiter.future.done():
                continue
            if await self.store.resolve_permission(request_id, decision="cancelled", by=by):
                await self.record(
                    "permission.resolved",
                    {
                        "request_id": request_id,
                        "decision": "cancelled",
                        "by": by,
                        "option_id": None,
                    },
                )
                self.manager.metrics.permission_resolutions.labels("cancelled", by).inc()
            if not waiter.future.done():
                waiter.future.set_result("cancelled")

    # ── updates and files (AgentHost) ───────────────────────────────────────
    async def on_update(self, update: dict[str, Any]) -> None:
        kind = str(update.get("sessionUpdate") or "")
        data = {k: v for k, v in update.items() if k != "sessionUpdate"}
        event_type = _UPDATE_EVENTS.get(kind)
        if event_type is None:
            await self.record("agent.update", {"kind": kind, "update": data})
        else:
            await self.record(event_type, data)

    def _display(self, path: Path) -> str:
        root = self.manager.settings.workspace_dir.resolve()
        return path.relative_to(root).as_posix() if path != root else "."

    def _confine(self, path: str) -> Path:
        try:
            return confine_path(self.manager.settings.workspace_dir, path)
        except PathError as exc:
            raise RpcError(INVALID_PARAMS, str(exc)) from exc

    async def read_text_file(self, path: str, line: int | None, limit: int | None) -> str:
        target = self._confine(path)
        if not target.is_file():
            raise RpcError(RESOURCE_NOT_FOUND, f"no such file: {path}")
        await self.record("file.read", {"path": self._display(target)})
        text = target.read_text(encoding="utf-8", errors="replace")
        if line is None and limit is None:
            return text
        lines = text.splitlines(keepends=True)
        start = max((line or 1) - 1, 0)
        end = start + limit if limit is not None else None
        return "".join(lines[start:end])

    async def write_text_file(self, path: str, content: str) -> None:
        target = self._confine(path)
        await self.record(
            "file.write", {"path": self._display(target), "bytes": len(content.encode("utf-8"))}
        )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Re-check once the parents exist: nothing may have redirected them.
            target = self._confine(str(target))
            target.write_text(content, encoding="utf-8")
        except (OSError, RpcError) as exc:
            message = exc.message if isinstance(exc, RpcError) else str(exc)
            await self.record(
                "file.write_failed", {"path": self._display(target), "error": message}
            )
            raise RpcError(INVALID_PARAMS, f"write failed: {message}") from exc


class SessionManager:
    def __init__(
        self,
        *,
        settings: HermarchusSettings,
        store: Store,
        broker: Broker,
        kinds: dict[str, AgentKind],
        metrics: Metrics,
    ) -> None:
        self.settings = settings
        self.store = store
        self.broker = broker
        self.kinds = kinds
        self.metrics = metrics
        self.live: dict[str, LiveSession] = {}
        self._record_locks: dict[str, asyncio.Lock] = {}
        self._create_lock = asyncio.Lock()

    async def record(self, session_id: str, type_: str, data: dict[str, Any] | None) -> Event:
        # Durable first, then fanned out — and per session in sequence order, so a reader
        # never sees seq N+1 before N.
        lock = self._record_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            event = await self.store.append_event(session_id, type_, data)
            self.broker.publish(event)
        self.metrics.events.labels(type_).inc()
        return event

    def forget(self, session_id: str) -> None:
        self.live.pop(session_id, None)
        self.metrics.sessions_live.set(len(self.live))

    def agent_path(self) -> str:
        env = build_child_env(
            agent_home=self.settings.agent_home,
            parent_env=os.environ,
            passthrough=self.settings.passthrough_names,
        )
        return env["PATH"]

    # ── startup / shutdown ──────────────────────────────────────────────────
    async def reconcile(self) -> list[str]:
        """Sessions a crash left live become ``interrupted``, each with an event."""
        ids = await self.store.live_session_ids()
        for session_id in ids:
            for request_id in await self.store.cancel_pending_permissions(
                [session_id], "host_restart"
            ):
                await self.record(
                    session_id,
                    "permission.resolved",
                    {
                        "request_id": request_id,
                        "decision": "cancelled",
                        "by": "host_restart",
                        "option_id": None,
                    },
                )
            for input_id in await self.store.drop_pending_inputs(session_id):
                await self.record(
                    session_id, "input.dropped", {"input_id": input_id, "reason": "host_restart"}
                )
            reason = "the host stopped while the session was live (crash or kill)"
            await self.store.update_session(session_id, status="interrupted", detail=reason)
            await self.record(session_id, "session.interrupted", {"reason": reason})
        return ids

    async def shutdown(self) -> None:
        sessions = list(self.live.values())
        await asyncio.gather(
            *(
                s.end(status="interrupted", event="session.interrupted", reason="host_shutdown")
                for s in sessions
            ),
            return_exceptions=True,
        )

    # ── the contract ────────────────────────────────────────────────────────
    async def create(
        self, *, agent: str, cwd: str, title: str | None, unattended: bool | None
    ) -> dict[str, Any]:
        kind = self.kinds.get(agent)
        if kind is None:
            raise NotFoundError(f"unknown agent kind {agent!r}")
        reason = kind.unavailable_reason(self.agent_path())
        if reason is not None:
            raise UnavailableError(f"agent kind {agent!r} is not available: {reason}")
        workdir, cwd_rel = resolve_workdir(self.settings.workspace_dir, cwd)
        async with self._create_lock:
            if len(self.live) >= self.settings.max_sessions:
                raise LimitError(
                    f"this instance already runs {len(self.live)} sessions, its limit "
                    f"(HERMARCHUS_MAX_SESSIONS={self.settings.max_sessions}); stop one first"
                )
            effective = self.settings.unattended if unattended is None else unattended
            row = await self.store.create_session(
                agent=agent, cwd=cwd_rel, title=title, unattended=effective
            )
            session = LiveSession(
                self,
                session_id=row["id"],
                kind=kind,
                workdir=workdir,
                cwd_rel=cwd_rel,
                unattended=effective,
            )
            self.live[row["id"]] = session
            self.metrics.sessions_live.set(len(self.live))
        self.metrics.sessions_created.labels(agent).inc()
        await self.record(
            row["id"],
            "session.created",
            {"agent": agent, "cwd": cwd_rel, "title": title, "unattended": effective},
        )
        session.launch()
        return await self.get(row["id"])

    async def get(self, session_id: str) -> dict[str, Any]:
        row = await self.store.get_session(session_id)
        if row is None:
            raise NotFoundError("no such session")
        live = self.live.get(session_id)
        row["turn_running"] = bool(live and live.turn_running)
        row["profile"] = row.pop("tenant_id")
        return row

    async def list_all(self) -> list[dict[str, Any]]:
        rows = await self.store.list_sessions()
        for row in rows:
            live = self.live.get(row["id"])
            row["turn_running"] = bool(live and live.turn_running)
            row["profile"] = row.pop("tenant_id")
        return rows

    def _live(self, session_id: str, row: dict[str, Any]) -> LiveSession:
        live = self.live.get(session_id)
        if live is None:
            raise ConflictError(
                "session_not_live", f"the session is {row['status']}; it has no agent running"
            )
        return live

    async def send_input(self, session_id: str, *, text: str, mode: str) -> dict[str, Any]:
        row = await self.get(session_id)
        live = self._live(session_id, row)
        item = await self.store.admit_input(session_id, mode=mode, text=text)
        event = await self.record(
            session_id, "input.admitted", {"input_id": item["id"], "mode": mode, "text": text}
        )
        if mode == "steer" and live.turn_running:
            # ACP has no way to add to a running turn, so a steer is delivered at the next
            # step boundary by cancelling the turn there (the agent stops at a safe point
            # and answers `cancelled`) and sending the steer as the next prompt, ahead of
            # anything queued.
            with contextlib.suppress(ConflictError):
                await live.cancel_turn("steer", input_id=item["id"])
        live.wake()
        return {"input_id": item["id"], "mode": mode, "seq": event.seq, "status": "pending"}

    async def cancel(self, session_id: str) -> Event | None:
        row = await self.get(session_id)
        live = self._live(session_id, row)
        await live.cancel_turn("controller")
        return None

    async def stop(self, session_id: str) -> dict[str, Any]:
        row = await self.get(session_id)
        live = self.live.get(session_id)
        if live is not None:
            await live.end(
                status="stopped", event="session.stopped", reason="stopped by controller"
            )
        elif row["status"] in ("starting", "idle", "running"):
            # A row that says live with no runner behind it — should not happen after
            # reconcile, but never leave it claiming to run.
            await self.store.update_session(session_id, status="stopped")
            await self.record(session_id, "session.stopped", {"reason": "stopped by controller"})
        return await self.get(session_id)

    async def delete(self, session_id: str) -> None:
        await self.get(session_id)
        if session_id in self.live:
            await self.stop(session_id)
        async with self._record_locks.setdefault(session_id, asyncio.Lock()):
            await self.store.delete_session(session_id)
        self.broker.close_session(session_id)
        self._record_locks.pop(session_id, None)

    async def permissions(self, session_id: str, *, pending_only: bool) -> list[dict[str, Any]]:
        await self.get(session_id)
        return await self.store.permissions(session_id, pending_only=pending_only)

    async def answer(self, session_id: str, request_id: str, decision: str) -> Event:
        row = await self.get(session_id)
        request = await self.store.get_permission(request_id)
        if request is None or request["session_id"] != session_id:
            raise NotFoundError("no such permission request in this session")
        if request["status"] != "pending":
            raise ConflictError(
                "permission_not_pending",
                f"this permission request was already resolved ({request['decision']})",
            )
        live = self._live(session_id, row)
        return await live.answer(request_id, decision)
