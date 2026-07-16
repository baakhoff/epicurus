"""Tests for the EpicurusModule MCP base."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from epicurus_core.manifest import CONTRACT_VERSION, ModelSlot, WritesDocument
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


async def test_manifest_carries_required_models() -> None:
    # A module can declare model slots the operator fills on the Modules page (#128).
    module = EpicurusModule(
        "embedder",
        required_models=[ModelSlot(key="embedding", role="embedding", label="Embedding model")],
    )
    manifest = await module.manifest()
    assert [s.key for s in manifest.required_models] == ["embedding"]
    assert manifest.required_models[0].role == "embedding"


async def test_manifest_required_models_defaults_empty() -> None:
    assert (await _greeter().manifest()).required_models == []


async def test_manifest_carries_reindexable() -> None:
    # A module that holds embeddings opts into the core's re-embed fan-out (#332).
    module = EpicurusModule("knowledge", reindexable=True)
    assert (await module.manifest()).reindexable is True


async def test_manifest_reindexable_defaults_false() -> None:
    assert (await _greeter().manifest()).reindexable is False


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


# ── writes_document declared through the decorator (#541, ADR-0100) ──────────


def _writer(*, tool_name: str | None = None, content_arg: str = "content") -> EpicurusModule:
    module = EpicurusModule("scribe", version="1.0.0")

    @module.tool(
        tool_name,
        writes_document=WritesDocument(
            content_arg=content_arg, title_arg="title", target_arg="path"
        ),
    )
    def write_doc(path: str, title: str, content: str) -> str:
        """Write a document."""
        return path

    return module


async def test_manifest_carries_a_tools_writes_document_annotation() -> None:
    # The decorator is how every module declares a tool, so it has to be how the annotation is
    # declared too — the model alone would be unreachable.
    manifest = await _writer().manifest()

    tool = next(t for t in manifest.tools if t.name == "write_doc")
    assert tool.writes_document is not None
    assert tool.writes_document.content_arg == "content"
    assert tool.writes_document.title_arg == "title"
    assert tool.writes_document.target_arg == "path"


async def test_annotation_keys_off_an_explicit_tool_name() -> None:
    # Naming the tool explicitly must not orphan the annotation from it.
    manifest = await _writer(tool_name="knowledge_create_doc").manifest()

    tool = next(t for t in manifest.tools if t.name == "knowledge_create_doc")
    assert tool.writes_document is not None
    assert tool.writes_document.content_arg == "content"


async def test_unannotated_tools_stay_unannotated() -> None:
    assert all(t.writes_document is None for t in (await _greeter().manifest()).tools)


async def test_annotation_is_checked_against_the_generated_input_schema() -> None:
    # The decorator derives input_schema from the signature, so a mis-named arg is catchable:
    # `body` is not a parameter of write_doc(path, title, content).
    with pytest.raises(ValidationError, match="body"):
        await _writer(content_arg="body").manifest()


async def test_annotating_a_tool_that_never_registered_is_an_error() -> None:
    # Otherwise the annotation is silently dropped and the pane is mysteriously dead.
    module = EpicurusModule("scribe")
    module._writes_documents["ghost_tool"] = WritesDocument(content_arg="content")

    with pytest.raises(ValueError, match="ghost_tool"):
        await module.manifest()


async def test_annotated_tools_still_run() -> None:
    _content, structured = await _writer().mcp.call_tool(
        "write_doc", {"path": "a.md", "title": "A", "content": "hi"}
    )
    assert structured == {"result": "a.md"}  # the annotation changes nothing about the call


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
