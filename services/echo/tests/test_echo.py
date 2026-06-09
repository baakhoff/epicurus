"""Unit tests for the echo module's MCP tool and manifest."""

from __future__ import annotations

from epicurus_core import CONTRACT_VERSION
from epicurus_echo.service import ECHO_SUBJECT, build_module


async def test_echo_tool_returns_message() -> None:
    content, structured = await build_module().mcp.call_tool("echo", {"message": "hello"})
    assert structured == {"result": "hello"}
    assert content[0].text == "hello"


async def test_manifest_lists_tool_and_event() -> None:
    manifest = await build_module().manifest()
    assert manifest.name == "echo"
    assert manifest.contract_version == CONTRACT_VERSION
    assert any(t.name == "echo" for t in manifest.tools)
    assert any(e.subject == ECHO_SUBJECT for e in manifest.events_consumed)
