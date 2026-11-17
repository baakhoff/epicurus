"""The ``echo`` reference agent — a real Agent Client Protocol agent over stdio.

Run as ``python -m epicurus_hermarchus.echo_agent``. It needs nothing but the standard
library and implements its side of the protocol on its own (it shares no code with the
client in ``acp.py``), so a session with it proves Hermarchus' adapter end to end. It is what
the tests and both smoke gates drive, and a diagnosis tool for an operator: if ``echo``
works and a vendor kind does not, the fault is in that program or its sign-in.

What it does with a prompt, by its first word (case-insensitive):

==========================  ==============================================================
``permission [what]``       announces a tool call and asks for permission; reports
                            ``permission: allowed (<option>)``, ``rejected`` or ``cancelled``
``write <path> <text…>``    writes the text through ``fs/write_text_file``
``read <path>``             reads a file through ``fs/read_text_file``
``tool [title]``            a tool call that goes pending → in progress → completed
``plan``                    publishes a three-step plan
``sleep <seconds>``         thinks, then waits (cancellable) — for steer, queue and cancel
``env``                     lists the *names* of its environment variables
``exit [code]``             exits at once (a crashed agent, for the failure paths)
``fail``                    answers the prompt with a JSON-RPC error
anything else               ``echo: <the prompt>``
==========================  ==============================================================

Paths are relative to the session's working directory unless absolute. Every turn ends with
``stopReason`` ``end_turn``, or ``cancelled`` when the client sent ``session/cancel``.

``ECHO_ACP_PROTOCOL_VERSION`` makes it answer ``initialize`` with another protocol version —
only so the version-negotiation refusal can be tested.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import sys
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
VERSION = "0.1.0"


class EchoAgent:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._calls = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._sessions: dict[str, Path] = {}
        self._cancelled: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._out = sys.stdout.buffer

    # ── framing ─────────────────────────────────────────────────────────────
    def _write(self, message: dict[str, Any]) -> None:
        self._out.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        self._out.flush()

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        rid = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await fut
        finally:
            self._pending.pop(rid, None)

    def _update(self, session_id: str, update: dict[str, Any]) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": session_id, "update": update},
            }
        )

    def _say(self, session_id: str, text: str, kind: str = "agent_message_chunk") -> None:
        self._update(session_id, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while True:
            raw = await reader.readline()
            if not raw:
                return  # the client closed our stdin: that is the stop signal
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg:
                task = asyncio.create_task(self._handle(msg))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            elif isinstance(msg.get("id"), int) and msg["id"] in self._pending:
                fut = self._pending[msg["id"]]
                if not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(str(msg["error"].get("message"))))
                    else:
                        fut.set_result(msg.get("result"))

    async def _handle(self, msg: dict[str, Any]) -> None:
        method, params, rid = msg["method"], msg.get("params") or {}, msg.get("id")
        try:
            result = await self._dispatch(method, params)
        except _RpcFailure as exc:
            if rid is not None:
                self._write(
                    {"jsonrpc": "2.0", "id": rid, "error": {"code": exc.code, "message": str(exc)}}
                )
            return
        if rid is not None:
            self._write({"jsonrpc": "2.0", "id": rid, "result": result})

    # ── methods ─────────────────────────────────────────────────────────────
    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            version = int(os.environ.get("ECHO_ACP_PROTOCOL_VERSION", PROTOCOL_VERSION))
            return {
                "protocolVersion": version,
                "agentCapabilities": {
                    "loadSession": False,
                    "promptCapabilities": {
                        "image": False,
                        "audio": False,
                        "embeddedContext": False,
                    },
                },
                "agentInfo": {"name": "hermarchus-echo", "title": "Echo", "version": VERSION},
                "authMethods": [],
            }
        if method == "session/new":
            cwd = params.get("cwd")
            if not isinstance(cwd, str) or not os.path.isabs(cwd):
                raise _RpcFailure(-32602, "cwd must be an absolute path")
            sid = f"echo-{len(self._sessions) + 1}"
            self._sessions[sid] = Path(cwd)
            self._cancelled[sid] = asyncio.Event()
            return {"sessionId": sid}
        if method == "session/cancel":
            cancel_id = params.get("sessionId")
            if isinstance(cancel_id, str) and cancel_id in self._cancelled:
                self._cancelled[cancel_id].set()
            return None
        if method == "session/prompt":
            return await self._prompt(params)
        raise _RpcFailure(-32601, f"method not found: {method}")

    async def _prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("sessionId")
        if sid not in self._sessions:
            raise _RpcFailure(-32602, "unknown session")
        cancelled = self._cancelled[sid]
        cancelled.clear()
        text = "".join(
            b.get("text", "")
            for b in params.get("prompt") or []
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        word, _, rest = text.partition(" ")
        word = word.lower()
        rest = rest.strip()
        handler = {
            "permission": self._permission,
            "write": self._write_file,
            "read": self._read_file,
            "tool": self._tool,
            "plan": self._plan,
            "sleep": self._sleep,
            "env": self._env,
            "exit": self._exit,
            "fail": self._fail,
        }.get(word)
        if handler is None:
            self._say(sid, f"echo: {text}")
            stop = "end_turn"
        else:
            stop = await handler(sid, rest)
        return {"stopReason": "cancelled" if cancelled.is_set() else stop}

    def _cwd(self, sid: str) -> Path:
        return self._sessions[sid]

    def _tool_call(self, sid: str, title: str, kind: str) -> str:
        call_id = f"call_{next(self._calls)}"
        self._update(
            sid,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": title,
                "kind": kind,
                "status": "pending",
            },
        )
        return call_id

    def _tool_done(self, sid: str, call_id: str, status: str, text: str) -> None:
        self._update(
            sid,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": status,
                "content": [{"type": "content", "content": {"type": "text", "text": text}}],
            },
        )

    async def _permission(self, sid: str, rest: str) -> str:
        title = f"Echo wants to: {rest or 'do something'}"
        call_id = self._tool_call(sid, title, "execute")
        result = await self._call(
            "session/request_permission",
            {
                "sessionId": sid,
                "toolCall": {"toolCallId": call_id, "title": title, "kind": "execute"},
                "options": [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        )
        outcome = (result or {}).get("outcome") or {}
        if outcome.get("outcome") == "selected":
            option = str(outcome.get("optionId"))
            if option.startswith("allow"):
                self._tool_done(sid, call_id, "completed", "done")
                self._say(sid, f"permission: allowed ({option})")
            else:
                self._tool_done(sid, call_id, "failed", "rejected")
                self._say(sid, f"permission: rejected ({option})")
        else:
            self._tool_done(sid, call_id, "failed", "cancelled")
            self._say(sid, "permission: cancelled")
        return "end_turn"

    def _path(self, sid: str, raw: str) -> str:
        p = Path(raw)
        return str(p if p.is_absolute() else self._cwd(sid) / p)

    async def _write_file(self, sid: str, rest: str) -> str:
        target, _, content = rest.partition(" ")
        path = self._path(sid, target)
        call_id = self._tool_call(sid, f"Write {target}", "edit")
        try:
            await self._call(
                "fs/write_text_file", {"sessionId": sid, "path": path, "content": content}
            )
        except RuntimeError as exc:
            self._tool_done(sid, call_id, "failed", str(exc))
            self._say(sid, f"write failed: {exc}")
        else:
            self._tool_done(sid, call_id, "completed", f"wrote {len(content)} characters")
            self._say(sid, f"wrote {target}")
        return "end_turn"

    async def _read_file(self, sid: str, rest: str) -> str:
        path = self._path(sid, rest)
        try:
            result = await self._call("fs/read_text_file", {"sessionId": sid, "path": path})
        except RuntimeError as exc:
            self._say(sid, f"read failed: {exc}")
        else:
            self._say(sid, f"read: {(result or {}).get('content', '')}")
        return "end_turn"

    async def _tool(self, sid: str, rest: str) -> str:
        call_id = self._tool_call(sid, rest or "Echo tool", "other")
        self._update(
            sid,
            {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "in_progress"},
        )
        self._tool_done(sid, call_id, "completed", "tool finished")
        self._say(sid, "tool: done")
        return "end_turn"

    async def _plan(self, sid: str, _rest: str) -> str:
        self._update(
            sid,
            {
                "sessionUpdate": "plan",
                "entries": [
                    {"content": "Read the prompt", "priority": "high", "status": "completed"},
                    {"content": "Echo it back", "priority": "medium", "status": "in_progress"},
                    {"content": "Stop", "priority": "low", "status": "pending"},
                ],
            },
        )
        self._say(sid, "plan: published")
        return "end_turn"

    async def _sleep(self, sid: str, rest: str) -> str:
        try:
            seconds = max(0.0, min(float(rest or "1"), 3600.0))
        except ValueError:
            seconds = 1.0
        self._say(sid, f"sleeping {seconds:g}s", kind="agent_thought_chunk")
        cancelled = self._cancelled[sid]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(cancelled.wait(), timeout=seconds)
        if cancelled.is_set():
            return "cancelled"
        self._say(sid, f"slept {seconds:g}s")
        return "end_turn"

    async def _env(self, sid: str, _rest: str) -> str:
        self._say(sid, "env: " + ",".join(sorted(os.environ)))
        return "end_turn"

    async def _exit(self, _sid: str, rest: str) -> str:
        sys.stdout.flush()
        os._exit(int(rest) if rest.strip().lstrip("-").isdigit() else 3)

    async def _fail(self, _sid: str, _rest: str) -> str:
        raise _RpcFailure(-32603, "echo was asked to fail")


class _RpcFailure(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(EchoAgent().run())


if __name__ == "__main__":
    main()
