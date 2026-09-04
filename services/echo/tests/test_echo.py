"""Unit tests for the echo module's MCP tool and manifest."""

from __future__ import annotations

from mcp.types import ContentBlock, TextContent

from epicurus_core import CONTRACT_VERSION
from epicurus_echo.service import (
    ECHO_PAGE_ID,
    ECHO_SUBJECT,
    build_module,
    echo_hover_card,
    echo_page,
)


def _text_of(item: ContentBlock) -> str:
    assert isinstance(item, TextContent)
    return item.text


async def test_echo_tool_returns_message() -> None:
    content, structured = await build_module().call_tool("echo", {"message": "hello"})
    assert structured == {"result": "hello"}
    assert _text_of(content[0]) == "hello"


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


async def test_manifest_version_matches_the_packaged_version() -> None:
    """The manifest hardcodes its version; nothing previously asserted it against
    ``pyproject.toml``, so it had silently drifted (0.5.0 vs 0.5.2, #845) — and the Modules
    page badge reads this value. Compare against the *declared* version rather than the
    installed metadata, which a stale editable install can lie about (mirrors
    calendar/websearch)."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    manifest = await build_module().manifest()
    assert manifest.version == declared


def test_echo_hover_card_matches_the_envelope_shape() -> None:
    card = echo_hover_card("event", "e1")
    assert card["title"] == "e1"
    assert any(detail["label"] == "kind" for detail in card["details"])
