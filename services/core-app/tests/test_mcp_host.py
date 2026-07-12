"""Unit tests for McpHost discovery URL and tool filtering (#126, #213).

The MCP connection itself is stubbed: ``streamablehttp_client`` is patched to record
the URL it is asked to open and then raise (URL-filter tests), or replaced with a mock
session that returns a canned tool listing (tool-filter tests), so ``discover``
exercises filtering logic without a live server.

The transport-hardening tests at the bottom (#472) are the exception — they run a *real*
FastMCP streamable-HTTP server, because the behavior they pin (a tool's isError, a refused
connection, and an RPC read timeout) only manifests through the live anyio task group the
mocks bypass.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

import epicurus_core_app.agent.mcp_host as mcp_host
from epicurus_core_app.agent.mcp_host import McpHost, ModuleUnreachableError, ToolCallError


def _recording_client() -> tuple[list[str], object]:
    """A ``streamablehttp_client`` stand-in: record the URL, then fail the connection."""
    seen: list[str] = []

    def fake(url: str) -> object:
        seen.append(url)
        raise RuntimeError("no server in tests")

    return seen, fake


async def test_discover_without_provider_scans_static_list() -> None:
    host = McpHost(["http://a:8080/mcp", "http://b:8080/mcp"])
    seen, fake = _recording_client()
    with patch("epicurus_core_app.agent.mcp_host.streamablehttp_client", side_effect=fake):
        specs, route = await host.discover()
    assert seen == ["http://a:8080/mcp", "http://b:8080/mcp"]
    assert specs == []
    assert route == {}


async def test_discover_uses_provider_when_wired() -> None:
    async def provider() -> list[str]:
        return ["http://enabled:8080/mcp"]

    # The static list would include a second module; the provider must override it.
    host = McpHost(["http://enabled:8080/mcp", "http://disabled:8080/mcp"])
    host.set_url_provider(provider)
    seen, fake = _recording_client()
    with patch("epicurus_core_app.agent.mcp_host.streamablehttp_client", side_effect=fake):
        await host.discover()
    assert seen == ["http://enabled:8080/mcp"]


async def test_discover_provider_returning_empty_scans_nothing() -> None:
    async def provider() -> list[str]:
        return []

    host = McpHost(["http://a:8080/mcp"], url_provider=provider)
    seen, fake = _recording_client()
    with patch("epicurus_core_app.agent.mcp_host.streamablehttp_client", side_effect=fake):
        specs, _ = await host.discover()
    assert seen == []
    assert specs == []


# ── Per-tool filter (#213) ────────────────────────────────────────────────────
# These tests replace the full MCP transport with a mock session that returns a
# known tool listing, so we can assert which names appear in specs / route.


def _mock_transport(tool_names: list[str]) -> tuple[object, object]:
    """Return (transport_cm, session_cm) mocks that advertise the given tool names."""
    tool_objs = []
    for name in tool_names:
        t = MagicMock()
        t.name = name
        t.description = ""
        t.inputSchema = {}
        tool_objs.append(t)

    listing = MagicMock()
    listing.tools = tool_objs

    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=listing)

    transport_cm = MagicMock()
    transport_cm.__aenter__ = AsyncMock(return_value=(None, None, None))
    transport_cm.__aexit__ = AsyncMock(return_value=False)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return transport_cm, session_cm


async def test_discover_without_filter_includes_all_tools() -> None:
    host = McpHost(["http://a:8080/mcp"])
    transport_cm, session_cm = _mock_transport(["tool_a", "tool_b"])

    with (
        patch(
            "epicurus_core_app.agent.mcp_host.streamablehttp_client",
            return_value=transport_cm,
        ),
        patch("epicurus_core_app.agent.mcp_host.ClientSession", return_value=session_cm),
    ):
        specs, _route = await host.discover()

    assert {s["function"]["name"] for s in specs} == {"tool_a", "tool_b"}
    assert set(_route) == {"tool_a", "tool_b"}


async def test_discover_with_tool_filter_excludes_disabled_tools() -> None:
    async def tool_filter() -> set[str]:
        return {"tool_b"}

    host = McpHost(["http://a:8080/mcp"])
    host.set_tool_filter(tool_filter)
    transport_cm, session_cm = _mock_transport(["tool_a", "tool_b"])

    with (
        patch(
            "epicurus_core_app.agent.mcp_host.streamablehttp_client",
            return_value=transport_cm,
        ),
        patch("epicurus_core_app.agent.mcp_host.ClientSession", return_value=session_cm),
    ):
        specs, route = await host.discover()

    assert {s["function"]["name"] for s in specs} == {"tool_a"}
    assert set(route) == {"tool_a"}


async def test_discover_with_empty_filter_includes_all_tools() -> None:
    async def tool_filter() -> set[str]:
        return set()

    host = McpHost(["http://a:8080/mcp"])
    host.set_tool_filter(tool_filter)
    transport_cm, session_cm = _mock_transport(["tool_a", "tool_b"])

    with (
        patch(
            "epicurus_core_app.agent.mcp_host.streamablehttp_client",
            return_value=transport_cm,
        ),
        patch("epicurus_core_app.agent.mcp_host.ClientSession", return_value=session_cm),
    ):
        specs, _ = await host.discover()

    assert {s["function"]["name"] for s in specs} == {"tool_a", "tool_b"}


# ── Tool errors surface, never swallow (#435) ─────────────────────────────────


def _call_transport(result: MagicMock) -> tuple[object, object]:
    """(transport_cm, session_cm) mocks whose ``call_tool`` returns *result*."""
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(return_value=result)

    transport_cm = MagicMock()
    transport_cm.__aenter__ = AsyncMock(return_value=(None, None, None))
    transport_cm.__aexit__ = AsyncMock(return_value=False)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return transport_cm, session_cm


def _tool_result(text: str, *, is_error: bool) -> MagicMock:
    block = MagicMock()
    block.text = text
    result = MagicMock()
    result.isError = is_error
    result.content = [block] if text else []
    return result


async def test_call_returns_text_on_success() -> None:
    host = McpHost([])
    transport_cm, session_cm = _call_transport(_tool_result("all good", is_error=False))
    with (
        patch(
            "epicurus_core_app.agent.mcp_host.streamablehttp_client",
            return_value=transport_cm,
        ),
        patch("epicurus_core_app.agent.mcp_host.ClientSession", return_value=session_cm),
    ):
        out = await host.call("some_tool", {}, "http://m:8080/mcp", tenant="t1")
    assert out == "all good"


async def test_call_raises_tool_call_error_on_iserror() -> None:
    # FastMCP reports a tool exception as an error *result*, not a transport error; the
    # host must raise so callers can tell failure from output — previously the error
    # text read as a successful call and the web closed the form as if it worked (#435).
    host = McpHost([])
    transport_cm, session_cm = _call_transport(
        _tool_result(
            "Error executing tool calendar_update_event: event 'e1' not found", is_error=True
        )
    )
    with (
        patch(
            "epicurus_core_app.agent.mcp_host.streamablehttp_client",
            return_value=transport_cm,
        ),
        patch("epicurus_core_app.agent.mcp_host.ClientSession", return_value=session_cm),
        pytest.raises(ToolCallError, match="event 'e1' not found"),
    ):
        await host.call("calendar_update_event", {}, "http://m:8080/mcp", tenant="t1")


async def test_call_error_without_text_gets_fallback_message() -> None:
    host = McpHost([])
    transport_cm, session_cm = _call_transport(_tool_result("", is_error=True))
    with (
        patch(
            "epicurus_core_app.agent.mcp_host.streamablehttp_client",
            return_value=transport_cm,
        ),
        patch("epicurus_core_app.agent.mcp_host.ClientSession", return_value=session_cm),
        pytest.raises(ToolCallError, match="'boom' failed"),
    ):
        await host.call("boom", {}, "http://m:8080/mcp", tenant="t1")


# ── Core built-in tools (ADR-0039) ────────────────────────────────────────────


def _spec(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {"name": name, "description": "", "parameters": {"type": "object"}},
    }


async def test_discover_includes_registered_builtin() -> None:
    host = McpHost([])  # no modules — only the built-in

    async def handler(_args: dict[str, object], _tenant: str) -> str:
        return "ok"

    host.register_builtin("now", _spec("now"), handler)
    specs, route = await host.discover()
    assert {s["function"]["name"] for s in specs} == {"now"}
    assert route["now"] == "__builtin__"


async def test_call_dispatches_builtin_in_process_with_tenant() -> None:
    host = McpHost([])

    async def handler(args: dict[str, object], tenant: str) -> str:
        return f"got {args.get('timezone')} for {tenant}"

    host.register_builtin("now", _spec("now"), handler)
    _, route = await host.discover()
    # the calling tenant is threaded through to the built-in handler
    out = await host.call("now", {"timezone": "UTC"}, route["now"], tenant="t1")
    assert out == "got UTC for t1"


async def test_memory_search_builtin_dispatches_end_to_end() -> None:
    # The real memory_search handler, registered + discovered + dispatched through the host —
    # proves the tool surface (spec → route → in-process call → tenant → formatted text), the
    # #523 "done means" that unit-testing the handler alone doesn't reach.
    from datetime import UTC, datetime

    from epicurus_core_app.agent.builtins import (
        MEMORY_SEARCH_SPEC,
        MEMORY_SEARCH_TOOL,
        make_memory_search_handler,
    )
    from epicurus_core_app.memory.memory import MemoryItem, SessionHit

    class _Searcher:
        def __init__(self) -> None:
            self.tenants: list[str] = []

        async def search_memory(
            self, *, tenant: str, query: str, limit: int = 20
        ) -> tuple[list[MemoryItem], int]:
            self.tenants.append(tenant)
            return [MemoryItem(id="f", text="Prefers restic", source="tool")], 1

        async def search_sessions(
            self, *, tenant: str, query: str, limit: int = 5
        ) -> list[SessionHit]:
            return [
                SessionHit(
                    session_id="s1",
                    title="Backups",
                    role="assistant",
                    snippet="use a nightly restic cron",
                    created_at=datetime(2026, 7, 4, tzinfo=UTC),
                )
            ]

    searcher = _Searcher()
    host = McpHost([])
    host.register_builtin(
        MEMORY_SEARCH_TOOL, MEMORY_SEARCH_SPEC, make_memory_search_handler(searcher)
    )
    specs, route = await host.discover()
    assert {s["function"]["name"] for s in specs} == {"memory_search"}
    out = await host.call("memory_search", {"query": "backup"}, route["memory_search"], tenant="t9")
    assert searcher.tenants == ["t9"]  # the calling tenant scoped the search (constraint #1)
    assert "Prefers restic" in out
    assert "Backups" in out


async def test_builtin_respects_disabled_filter() -> None:
    async def tool_filter() -> set[str]:
        return {"now"}

    async def handler(_args: dict[str, object], _tenant: str) -> str:
        return "ok"

    host = McpHost([])
    host.set_tool_filter(tool_filter)
    host.register_builtin("now", _spec("now"), handler)
    specs, route = await host.discover()
    assert specs == []
    assert "now" not in route


# ── Transport hardening: real server, real failure modes (#472) ────────────────
# ``call`` dispatches every board/calendar UI action. The mocked tests above cannot
# reproduce what a live streamable-HTTP connection does: it runs its transport in an anyio
# task group, so a failure raised *inside* the ``async with`` — a refused/dropped socket, an
# RPC read timeout, *or* a tool's isError — escapes wrapped in a (possibly nested)
# ``ExceptionGroup``, never a bare exception. These tests run a real FastMCP server so the
# host's contract is verified against production behavior: a tool error surfaces as a bare
# ``ToolCallError`` (raised after the block, so it is never wrapped), while a genuinely
# unreachable module normalizes to ``ModuleUnreachableError`` — the structured signal
# ``ModuleRegistry.invoke`` maps to a controlled 502 instead of a raw ``NetworkError``.


def _free_port() -> int:
    """An ephemeral localhost port — bind :0, read it back, release it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _live_module_app() -> object:
    """A FastMCP streamable-HTTP app with a ``echo`` tool and a raising ``boom`` tool."""
    mcp = FastMCP("test-module")

    @mcp.tool()
    def echo(message: str) -> str:
        return f"echo: {message}"

    @mcp.tool()
    def boom() -> str:
        raise ValueError("event 'e1' not found")

    return mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def _live_module() -> AsyncIterator[str]:
    """Serve the module in-process on an ephemeral port; yield its ``/mcp`` URL.

    A plain ``async with`` helper rather than a fixture — the repo has no async-fixture
    convention and this keeps the lifecycle explicit and portable (it boots on Windows and
    Linux alike, no Docker).
    """
    port = _free_port()
    config = uvicorn.Config(_live_module_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        for _ in range(250):  # up to ~5s for startup
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - only trips if uvicorn never comes up
            raise RuntimeError("uvicorn did not start in time")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await serve_task


async def test_call_success_against_live_server() -> None:
    host = McpHost([])
    async with _live_module() as url:
        out = await host.call("echo", {"message": "hi"}, url, tenant="t1")
    assert out == "echo: hi"


async def test_call_iserror_surfaces_bare_toolcallerror() -> None:
    # The crux of #472: a tool's isError is detected inside the streamable client's anyio
    # task group. Were ToolCallError raised there, anyio would wrap it in an ExceptionGroup
    # and every ``except ToolCallError`` (agent loop + ModuleRegistry.invoke) would silently
    # miss it. The host raises it *after* the block, so it must arrive bare here — this fails
    # if someone moves the isError check back inside the ``async with``.
    host = McpHost([])
    async with _live_module() as url:
        with pytest.raises(ToolCallError, match="event 'e1' not found"):
            await host.call("boom", {}, url, tenant="t1")


async def test_call_refused_module_raises_module_unreachable() -> None:
    # Nothing listening on the port → the transport throws a nested
    # ExceptionGroup(ConnectError); the host must normalize it to ModuleUnreachableError,
    # not let the raw group escape toward nginx as an opaque NetworkError (#472).
    host = McpHost([])
    dead_url = f"http://127.0.0.1:{_free_port()}/mcp"
    with pytest.raises(ModuleUnreachableError):
        await host.call("echo", {"message": "hi"}, dead_url, tenant="t1")


async def test_call_hung_module_times_out_to_module_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A module that accepts the TCP connection but never replies must not hang ``call``
    # forever — the original #472 defect (no read-timeout override). The bounded RPC read
    # trips and normalizes to ModuleUnreachableError. Shorten the bound so the test is fast;
    # wrap in wait_for so a regression fails loudly at 15s rather than at the 60s gate.
    monkeypatch.setattr(mcp_host, "_CALL_TIMEOUT_S", 1.0)

    async def _hold(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.sleep(30)  # accept the socket, send nothing back
        finally:
            writer.close()

    port = _free_port()
    server = await asyncio.start_server(_hold, "127.0.0.1", port)
    host = McpHost([])
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        async with server:
            with pytest.raises(ModuleUnreachableError):
                await asyncio.wait_for(
                    host.call("echo", {"message": "hi"}, url, tenant="t1"),
                    timeout=15,
                )
    finally:
        server.close()
