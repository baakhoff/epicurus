"""MCP host — the core connects to module MCP servers to discover and call tools.

Per ADR-0004 the agent (here) is the MCP *host*: modules expose tools over MCP and the
agent calls them. A fresh connection is made per operation — simple, and always
reflects the modules currently running.
"""

from __future__ import annotations

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

    def __init__(self, module_urls: list[str]) -> None:
        self._module_urls = list(module_urls)

    async def discover(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Return ``(OpenAI tool specs, tool-name -> module-URL route)``."""
        specs: list[dict[str, Any]] = []
        route: dict[str, str] = {}
        for url in self._module_urls:
            try:
                async with (
                    streamablehttp_client(url) as (read, write, _),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    listing = await session.list_tools()
                    for tool in listing.tools:
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
