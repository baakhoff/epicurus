"""Tests for the EpicurusModule MCP base."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from epicurus_core.manifest import CONTRACT_VERSION
from epicurus_core.module import EpicurusModule, add_manifest_route


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


def test_mcp_is_reachable_for_clients() -> None:
    # The MCP endpoint must be reachable by an MCP client over the internal network:
    # served at the app root (so mounting under "/mcp" is not double-prefixed to
    # "/mcp/mcp") with DNS-rebinding protection off (it rejects service hostnames like
    # "echo:8080" with HTTP 421 — the contract is local-only, ADR-0004).
    settings = _greeter().mcp.settings
    assert settings.streamable_http_path == "/"
    assert settings.transport_security.enable_dns_rebinding_protection is False


def test_manifest_route_serves_the_manifest() -> None:
    # The core's module registry (and the web shell) read each module's GET /manifest.
    app = FastAPI()
    add_manifest_route(app, _greeter())
    response = TestClient(app).get("/manifest")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "greeter"
    assert body["version"] == "1.0.0"
    assert any(tool["name"] == "greet" for tool in body["tools"])
