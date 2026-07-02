"""Unit tests for McpHost discovery URL and tool filtering (#126, #213).

The MCP connection itself is stubbed: ``streamablehttp_client`` is patched to record
the URL it is asked to open and then raise (URL-filter tests), or replaced with a mock
session that returns a canned tool listing (tool-filter tests), so ``discover``
exercises filtering logic without a live server.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from epicurus_core_app.agent.mcp_host import McpHost, ToolCallError


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
