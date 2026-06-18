"""MCP host — the core connects to module MCP servers to discover and call tools.

Per ADR-0004 the agent (here) is the MCP *host*: modules expose tools over MCP and the
agent calls them. A fresh connection is made per operation — simple, and always
reflects the modules currently running.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from epicurus_core import get_logger

log = get_logger("epicurus_core_app.agent.mcp")


def _text(content: list[Any]) -> str:
    """Join the text blocks of an MCP tool result."""
    parts = [text for block in content if (text := getattr(block, "text", None))]
    return "\n".join(parts)


class McpHost:
    """Discovers and calls tools on the configured module MCP servers."""

    def __init__(
        self,
        module_urls: list[str],
        *,
        url_provider: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> None:
        self._module_urls = list(module_urls)
        # When set, discovery asks this for the live set of enabled-module MCP URLs, so a
        # disabled module's tools are never offered to the model (#126). Without it, the
        # static configured list is scanned (back-compatible default).
        self._url_provider = url_provider
        # When set, discovery calls this to get the flat set of per-tool disabled names
        # across all enabled modules (#213). Tools in the set are skipped regardless of
        # whether their module URL is included.
        self._tool_filter: Callable[[], Awaitable[set[str]]] | None = None

    def set_url_provider(self, provider: Callable[[], Awaitable[list[str]]]) -> None:
        """Wire the live enabled-modules URL source.

        A setter (rather than a constructor arg) avoids a construction cycle: the registry
        needs this host, and the host needs the registry's ``enabled_mcp_urls`` (#126).
        """
        self._url_provider = provider

    def set_tool_filter(self, provider: Callable[[], Awaitable[set[str]]]) -> None:
        """Wire the per-tool disabled-names source (#213).

        A setter (rather than a constructor arg) avoids the same construction cycle as
        ``set_url_provider``. The provider returns the flat set of tool names the operator
        has disabled; ``discover`` skips any tool whose name is in that set.
        """
        self._tool_filter = provider

    async def discover(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Return ``(OpenAI tool specs, tool-name -> module-URL route)``.

        Only **enabled** modules are scanned when a ``url_provider`` is wired (#126).
        Individually disabled tools are skipped when a ``tool_filter`` is wired (#213).
        """
        urls = await self._url_provider() if self._url_provider is not None else self._module_urls
        disabled = await self._tool_filter() if self._tool_filter is not None else set()
        specs: list[dict[str, Any]] = []
        route: dict[str, str] = {}
        for url in urls:
            try:
                async with (
                    streamablehttp_client(url) as (read, write, _),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    listing = await session.list_tools()
                    for tool in listing.tools:
                        if tool.name in disabled:
                            continue
                        specs.append(
                            {
                                "type": "function",
                                "function": {
                                    "name": tool.name,
                                    "description": tool.description or "",
                                    "parameters": tool.inputSchema
                                    or {"type": "object", "properties": {}},
                                },
                            }
                        )
                        route[tool.name] = url
            except Exception:
                log.warning("mcp discovery failed", url=url, exc_info=True)
        return specs, route

    async def call(self, name: str, arguments: dict[str, Any], url: str) -> str:
        """Call ``name`` on the module at ``url`` and return its text result."""
        async with (
            streamablehttp_client(url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
            return _text(result.content)
