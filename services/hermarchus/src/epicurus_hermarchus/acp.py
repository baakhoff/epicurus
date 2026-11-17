"""The Agent Client Protocol, client side — one agent program per session, over stdio.

ACP (https://agentclientprotocol.com) is JSON-RPC 2.0 with one message per line on the
agent's stdin/stdout. Hermarchus is the *client*: it starts the program, negotiates the
protocol version (``initialize``), opens one ACP session (``session/new``) in the session's
working directory, sends each turn as ``session/prompt`` and cancels with ``session/cancel``.
The agent calls back for ``session/request_permission`` and the client file methods
``fs/read_text_file`` / ``fs/write_text_file``, and streams ``session/update`` notifications
(message and thought chunks, tool calls and their updates, plans).

The framing is implemented here rather than taken from the official Python SDK
(``agent-client-protocol`` on PyPI). The protocol surface Hermarchus needs is small and
stable (v1), the SDK's 1.0.0 was a week old when this was written and its typing under
``mypy --strict`` unconfirmed, and owning the framing keeps the seam auditable in one file.
The built-in ``echo`` agent implements its side independently (``echo_agent.py``), so the
two halves are not one implementation agreeing with itself.

``terminal/*`` is not offered (``clientCapabilities.terminal = false``): agents run their own
commands inside the sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog

from epicurus_hermarchus import __version__

__all__ = [
    "PROTOCOL_VERSION",
    "AcpAgent",
    "AgentExited",
    "AgentHost",
    "AgentStartError",
    "JsonRpcConnection",
    "RpcError",
]

log = structlog.get_logger("hermarchus.acp")

# The stable protocol version this client speaks (`initialize.protocolVersion`).
PROTOCOL_VERSION = 1

# JSON-RPC / ACP error codes.
PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
AUTH_REQUIRED = -32000
RESOURCE_NOT_FOUND = -32002

# One ACP message may carry a whole file (fs/write_text_file); the default 64 KiB line limit
# of an asyncio StreamReader would cut it.
_LINE_LIMIT = 64 * 1024 * 1024


class RpcError(Exception):
    """A JSON-RPC error — raised by a handler to answer one, or received from the peer."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def as_error(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


class AgentExited(Exception):
    """The agent process closed its stdout (exited) while a request was outstanding."""


class AgentStartError(Exception):
    """The agent could not be started, or refused to open a session."""


RequestHandler = Callable[[str, Any], Awaitable[Any]]
NotificationHandler = Callable[[str, Any], Awaitable[None]]


class JsonRpcConnection:
    """Newline-delimited JSON-RPC 2.0 over a pair of streams, both directions.

    Notifications from the peer are handled **in order** on the reader task (so the session
    log keeps the agent's order); requests from the peer each get their own task, because a
    permission request may wait minutes for a human and must not stall the stream.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_request: RequestHandler,
        on_notification: NotificationHandler,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_request = on_request
        self._on_notification = on_notification
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()

    def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop(), name="acp-reader")

    async def _send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
        async with self._write_lock:
            if self._writer.is_closing():
                raise AgentExited("the agent's stdin is closed")
            self._writer.write(line.encode("utf-8"))
            try:
                await self._writer.drain()
            except (ConnectionError, BrokenPipeError) as exc:
                raise AgentExited("the agent's stdin is closed") from exc

    async def request(self, method: str, params: Any) -> Any:
        if self.closed.is_set():
            raise AgentExited("the agent has exited")
        self._next_id += 1
        rid = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Any) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _respond(self, rid: Any, method: str, params: Any) -> None:
        try:
            result = await self._on_request(method, params)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "result": result}
        except RpcError as exc:
            reply = {"jsonrpc": "2.0", "id": rid, "error": exc.as_error()}
        except Exception as exc:  # a handler bug must still answer, or the agent hangs
            log.warning("acp.handler_failed", method=method, error=str(exc))
            reply = {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": INTERNAL_ERROR, "message": str(exc) or type(exc).__name__},
            }
        with contextlib.suppress(AgentExited):
            await self._send(reply)

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    raw = await self._reader.readline()
                except ValueError:  # a line over the limit: the stream is unusable
                    log.warning("acp.line_too_long")
                    break
                if not raw:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Agents that print logs to stdout break the protocol; skip the line.
                    log.warning("acp.unparseable_line", line=line[:200].decode("utf-8", "replace"))
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._dispatch(msg)
        finally:
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(AgentExited("the agent exited"))

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if isinstance(method, str):
            if "id" in msg:
                task = asyncio.create_task(self._respond(msg["id"], method, msg.get("params")))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                try:
                    await self._on_notification(method, msg.get("params"))
                except Exception as exc:
                    log.warning("acp.notification_failed", method=method, error=str(exc))
            return
        rid = msg.get("id")
        future = self._pending.get(rid) if isinstance(rid, int) else None
        if future is None or future.done():
            return
        if "error" in msg and isinstance(msg["error"], dict):
            err = msg["error"]
            future.set_exception(
                RpcError(
                    int(err.get("code", INTERNAL_ERROR)), str(err.get("message")), err.get("data")
                )
            )
        else:
            future.set_result(msg.get("result"))

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        self.closed.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AgentExited("the connection was closed"))


class AgentHost(Protocol):
    """What a session gives its agent: where updates go and how callbacks are answered."""

    async def on_update(self, update: dict[str, Any]) -> None: ...

    async def request_permission(
        self, tool_call: dict[str, Any], options: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Return an ACP ``RequestPermissionOutcome``."""
        ...

    async def read_text_file(self, path: str, line: int | None, limit: int | None) -> str: ...

    async def write_text_file(self, path: str, content: str) -> None: ...


@dataclass
class AgentInfo:
    protocol_version: int
    agent_info: dict[str, Any] | None
    agent_capabilities: dict[str, Any]
    auth_methods: list[dict[str, Any]]
    agent_session_id: str


@dataclass
class AcpAgent:
    """One agent program, started for one session, spoken to over ACP.

    This is the single adapter every agent kind goes through — the built-in ``echo`` agent
    included, so the reference agent proves this path rather than a special case.
    """

    argv: list[str]
    cwd: Path
    env: Mapping[str, str]
    host: AgentHost
    start_timeout_s: float = 120.0
    process: asyncio.subprocess.Process | None = None
    info: AgentInfo | None = None
    _conn: JsonRpcConnection | None = None
    _stderr: deque[str] = field(default_factory=lambda: deque(maxlen=40))
    _stderr_task: asyncio.Task[None] | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> AgentInfo:
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(self.cwd),
                env=dict(self.env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Its own process group, so stopping it also stops whatever it spawned
                # (npx wrappers, the vendor CLI behind an adapter).
                start_new_session=True,
                limit=_LINE_LIMIT,
            )
        except OSError as exc:
            raise AgentStartError(f"could not start {self.argv[0]!r}: {exc}") from exc
        assert self.process.stdout is not None and self.process.stdin is not None
        assert self.process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr(self.process.stderr))
        self._conn = JsonRpcConnection(
            self.process.stdout,
            self.process.stdin,
            on_request=self._on_request,
            on_notification=self._on_notification,
        )
        self._conn.start()
        try:
            self.info = await asyncio.wait_for(self._handshake(), timeout=self.start_timeout_s)
        except TimeoutError as exc:
            raise AgentStartError(
                f"the agent did not finish starting within {self.start_timeout_s:g}s"
            ) from exc
        except AgentExited as exc:
            await self._wait_exit(2.0)
            raise AgentStartError(
                f"the agent exited during start (exit code {self.returncode})"
            ) from exc
        return self.info

    async def _handshake(self) -> AgentInfo:
        assert self._conn is not None
        init = await self._conn.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": False,
                },
                "clientInfo": {"name": "hermarchus", "title": "Hermarchus", "version": __version__},
            },
        )
        if not isinstance(init, dict):
            raise AgentStartError("the agent's initialize response is not an object")
        version = init.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            # The agent answered with the version *it* supports; we speak only ours.
            raise AgentStartError(
                f"the agent speaks ACP protocol version {version!r}; this host speaks "
                f"{PROTOCOL_VERSION}"
            )
        auth_methods = init.get("authMethods") or []
        try:
            created = await self._conn.request(
                "session/new", {"cwd": str(self.cwd), "mcpServers": []}
            )
        except RpcError as exc:
            if exc.code == AUTH_REQUIRED:
                names = ", ".join(str(m.get("name") or m.get("id")) for m in auth_methods) or "?"
                raise AgentStartError(
                    "the agent needs a sign-in before it can open a session (methods: "
                    f"{names}). Sign in once inside this Hermarchus instance, as its user, "
                    "with the program's own login command."
                ) from exc
            raise AgentStartError(f"session/new was refused: {exc.message}") from exc
        if not isinstance(created, dict) or not isinstance(created.get("sessionId"), str):
            raise AgentStartError("the agent's session/new response has no sessionId")
        agent_info = init.get("agentInfo")
        return AgentInfo(
            protocol_version=PROTOCOL_VERSION,
            agent_info=agent_info if isinstance(agent_info, dict) else None,
            agent_capabilities=init.get("agentCapabilities") or {},
            auth_methods=list(auth_methods),
            agent_session_id=created["sessionId"],
        )

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        with contextlib.suppress(Exception):
            while True:
                raw = await stream.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip()
                self._stderr.append(line)
                log.debug("agent.stderr", line=line[:500])

    @property
    def returncode(self) -> int | None:
        return self.process.returncode if self.process else None

    def stderr_tail(self) -> list[str]:
        return list(self._stderr)

    async def wait(self) -> int:
        assert self.process is not None
        return await self.process.wait()

    async def _wait_exit(self, timeout: float) -> bool:
        if self.process is None:
            return True
        try:
            await asyncio.wait_for(self.process.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    def _signal(self, sig: int) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, sig)

    async def close(self, grace_s: float = 5.0) -> int | None:
        """Stop the program: close stdin, then SIGTERM its group, then SIGKILL."""
        if self.process is None:
            return None
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
        if not await self._wait_exit(min(1.0, grace_s)):
            self._signal(signal.SIGTERM)
            if not await self._wait_exit(grace_s):
                self._signal(signal.SIGKILL)
                await self._wait_exit(5.0)
        else:
            # The leader exited on EOF; make sure nothing it spawned outlives it.
            self._signal(signal.SIGKILL)
        if self._conn is not None:
            await self._conn.close()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
        return self.returncode

    # ── a turn ──────────────────────────────────────────────────────────────
    async def prompt(self, text: str) -> str:
        """Run one turn; return the agent's ``stopReason``."""
        assert self._conn is not None and self.info is not None
        result = await self._conn.request(
            "session/prompt",
            {
                "sessionId": self.info.agent_session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        )
        reason = result.get("stopReason") if isinstance(result, dict) else None
        return str(reason or "end_turn")

    async def cancel(self) -> None:
        if self._conn is None or self.info is None:
            return
        with contextlib.suppress(AgentExited):
            await self._conn.notify("session/cancel", {"sessionId": self.info.agent_session_id})

    # ── the agent calling us ────────────────────────────────────────────────
    def _check_session(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        if self.info is not None and params.get("sessionId") != self.info.agent_session_id:
            raise RpcError(INVALID_PARAMS, "unknown sessionId")
        return params

    async def _on_request(self, method: str, params: Any) -> Any:
        if method == "session/request_permission":
            p = self._check_session(params)
            tool_call = p.get("toolCall") if isinstance(p.get("toolCall"), dict) else {}
            options = [o for o in (p.get("options") or []) if isinstance(o, dict)]
            outcome = await self.host.request_permission(tool_call or {}, options)
            return {"outcome": outcome}
        if method == "fs/read_text_file":
            p = self._check_session(params)
            path = p.get("path")
            if not isinstance(path, str):
                raise RpcError(INVALID_PARAMS, "path is required")
            line = p.get("line") if isinstance(p.get("line"), int) else None
            limit = p.get("limit") if isinstance(p.get("limit"), int) else None
            return {"content": await self.host.read_text_file(path, line, limit)}
        if method == "fs/write_text_file":
            p = self._check_session(params)
            path, content = p.get("path"), p.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                raise RpcError(INVALID_PARAMS, "path and content are required")
            await self.host.write_text_file(path, content)
            return {}
        raise RpcError(METHOD_NOT_FOUND, f"method not supported by this client: {method}")

    async def _on_notification(self, method: str, params: Any) -> None:
        if method != "session/update" or not isinstance(params, dict):
            return
        if self.info is not None and params.get("sessionId") != self.info.agent_session_id:
            return
        update = params.get("update")
        if isinstance(update, dict):
            await self.host.on_update(update)
