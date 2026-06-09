"""Tests for the EpicurusModule MCP base."""

from __future__ import annotations

from epicurus_core.manifest import CONTRACT_VERSION
from epicurus_core.module import EpicurusModule


def _greeter() -> EpicurusModule:
    module = EpicurusModule("greeter", version="1.0.0", description="says hi")

    @module.tool()
    def greet(name: str) -> str:
        """Greet someone."""
        return f"hello {name}"

    module.emits("greeting.sent", "after a greeting")
    module.consumes("inbox.message", "incoming messages")
    return module


async def test_manifest_reflects_tools_and_events() -> None:
    manifest = await _greeter().manifest(secrets=["API_KEY"])

    assert manifest.name == "greeter"
    assert manifest.version == "1.0.0"
    assert manifest.contract_version == CONTRACT_VERSION

    tool = next(t for t in manifest.tools if t.name == "greet")
    assert tool.description == "Greet someone."
    assert tool.input_schema["properties"]["name"]["type"] == "string"

    assert manifest.events_emitted[0].subject == "greeting.sent"
    assert manifest.events_consumed[0].subject == "inbox.message"
    assert manifest.secrets == ["API_KEY"]


async def test_tool_is_callable() -> None:
    content, structured = await _greeter().mcp.call_tool("greet", {"name": "ada"})
    assert structured == {"result": "hello ada"}
    assert content[0].text == "hello ada"


def test_http_app_builds() -> None:
    app = _greeter().http_app()
    assert hasattr(app, "routes")
