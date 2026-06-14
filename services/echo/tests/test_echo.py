"""Unit tests for the echo module's MCP tool and manifest."""

from __future__ import annotations

from epicurus_core import CONTRACT_VERSION
from epicurus_echo.service import (
    ECHO_PAGE_ID,
    ECHO_SUBJECT,
    build_module,
    echo_hover_card,
    echo_page,
)


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


async def test_manifest_declares_a_browser_page() -> None:
    manifest = await build_module().manifest()
    page = next(p for p in manifest.pages if p.id == ECHO_PAGE_ID)
    assert page.archetype == "browser"
    assert page.title == "Echoes"


def test_echo_page_data_matches_the_browser_shape() -> None:
    data = echo_page()
    assert data["title"] == "Echoes"
    assert len(data["items"]) >= 1
    first = data["items"][0]
    assert {"id", "title", "subtitle", "body"} <= first.keys()


async def test_manifest_declares_a_resolver() -> None:
    assert (await build_module().manifest()).resolver is True


def test_echo_hover_card_matches_the_envelope_shape() -> None:
    card = echo_hover_card("event", "e1")
    assert card["title"] == "e1"
    assert any(detail["label"] == "kind" for detail in card["details"])
