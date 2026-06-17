"""Unit tests for McpHost discovery URL selection (#126).

The MCP connection itself is stubbed: ``streamablehttp_client`` is patched to record
the URL it is asked to open and then raise, so ``discover`` exercises exactly which
modules it scans (enabled-only when a ``url_provider`` is wired) without a live server.
"""

from __future__ import annotations

from unittest.mock import patch

from epicurus_core_app.agent.mcp_host import McpHost


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
