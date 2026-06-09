"""MCP module base — the building block for a sidecar module's tool contract.

Wraps the MCP SDK's ``FastMCP`` with epicurus conventions: register tools, declare
the events the module emits/consumes, generate the module manifest, and expose the
HTTP (streamable-http) app to serve over the internal Docker network. The contract
is local-only (see docs/ARCHITECTURE.md trust boundary).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette

from epicurus_core.manifest import CONTRACT_VERSION, EventSpec, ModuleManifest, ToolSpec

__all__ = ["EpicurusModule"]

Decorator = Callable[[Callable[..., Any]], Callable[..., Any]]


class EpicurusModule:
    """A sidecar module: MCP tools exposed to the agent, plus declared events.

    >>> module = EpicurusModule("greeter", version="1.0.0")
    >>> @module.tool()
    ... def greet(name: str) -> str:
    ...     return f"hello {name}"
    """

    def __init__(
        self,
        name: str,
        *,
        version: str = "0.1.0",
        description: str = "",
        instructions: str | None = None,
        image: str | None = None,
    ) -> None:
        self._name = name
        self._version = version
        self._description = description
        self._image = image
        self._mcp = FastMCP(name, instructions=instructions)
        self._events_emitted: list[EventSpec] = []
        self._events_consumed: list[EventSpec] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def mcp(self) -> FastMCP:
        """The underlying FastMCP server (for advanced use / testing)."""
        return self._mcp

    def tool(self, name: str | None = None, description: str | None = None) -> Decorator:
        """Register a tool (decorator). Delegates to FastMCP."""
        return self._mcp.tool(name=name, description=description)

    def emits(self, subject: str, description: str = "") -> None:
        """Declare a base event subject this module publishes."""
        self._events_emitted.append(EventSpec(subject=subject, description=description))

    def consumes(self, subject: str, description: str = "") -> None:
        """Declare a base event subject this module subscribes to."""
        self._events_consumed.append(EventSpec(subject=subject, description=description))

    async def manifest(
        self,
        *,
        config: list[str] | None = None,
        secrets: list[str] | None = None,
    ) -> ModuleManifest:
        """Build the manifest from the registered tools and declared events."""
        tools = [
            ToolSpec(name=t.name, description=t.description or "", input_schema=t.inputSchema)
            for t in await self._mcp.list_tools()
        ]
        return ModuleManifest(
            name=self._name,
            version=self._version,
            description=self._description,
            contract_version=CONTRACT_VERSION,
            image=self._image,
            tools=tools,
            events_emitted=list(self._events_emitted),
            events_consumed=list(self._events_consumed),
            config=config or [],
            secrets=secrets or [],
        )

    def http_app(self) -> Starlette:
        """ASGI app serving MCP over streamable HTTP (internal Docker network only)."""
        return self._mcp.streamable_http_app()
