"""MCP host — the core connects to module MCP servers to discover and call tools.

Per ADR-0004 the agent (here) is the MCP *host*: modules expose tools over MCP and the
agent calls them. A fresh connection is made per operation — simple, and always
reflects the modules currently running.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError

from epicurus_core import get_logger

log = get_logger("epicurus_core_app.agent.mcp")

# Sentinel route for a core built-in tool — dispatched in-process by ``call`` rather than
# over MCP to a module (ADR-0039).
_BUILTIN_URL = "__builtin__"

# Bound for a single module tool call — the HTTP request timeout *and* the MCP RPC read
# timeout (initialize + call_tool). Without it a module that accepts the connection but
# never replies would hang ``call`` forever, and the board/calendar action behind it would
# stall until the browser gave up with a raw ``NetworkError`` (#472). 30s is generous — a
# tool may make an external round-trip (a Google write) — but finite, matching the
# ``_post_json`` write bound in ``modules.py``.
_CALL_TIMEOUT_S = 30.0

#: A built-in tool: its OpenAI function spec + an async handler ``(arguments, tenant) -> text``.
#: The tenant is passed so a built-in can read or write tenant-scoped state (e.g. ``remember``).
BuiltinHandler = Callable[[dict[str, Any], str], Awaitable[str]]
BuiltinTool = tuple[dict[str, Any], BuiltinHandler]


class ToolCallError(Exception):
    """A module tool ran and reported failure — the MCP result carried ``isError``.

    FastMCP wraps a tool exception as an error *result* (not a transport error), so
    without this check a failed tool read as a successful call whose text happened to
    be the error message — the web closed the form as if the action worked (#435).
    ``str(exc)`` is the tool's own message (e.g. ``event 'x' not found``).

    Distinct from :class:`ModuleUnreachableError`: here the tool *ran* and rejected the
    request (a 4xx-shaped, caller-facing failure); there the module never answered.
    """


class ModuleUnreachableError(Exception):
    """The module could not be reached or did not answer in time — no tool logic ran.

    Raised when the MCP transport to the module refuses, drops, or exceeds
    :data:`_CALL_TIMEOUT_S` (a connection error, or an RPC read timeout on
    ``initialize`` / ``call_tool``). The streamable-HTTP client runs its transport in an
    anyio task group, so such a failure surfaces as an ``ExceptionGroup``, never a bare
    ``httpx``/``McpError`` — :meth:`McpHost.call` normalizes all of those into this one
    type so callers have a stable, layer-appropriate contract. The HTTP layer
    (``ModuleRegistry.invoke``) maps it to a controlled **502**, so a board/calendar
    action against a down module shows a reason instead of a raw ``NetworkError`` (#472);
    the agent's tool loop reports it to the model rather than crashing the turn.
    """


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
        # Core built-in tools (ADR-0039): name -> (spec, handler), offered alongside the
        # modules' tools and dispatched in-process (no HTTP). Empty by default.
        self._builtins: dict[str, BuiltinTool] = {}

    def register_builtin(self, name: str, spec: dict[str, Any], handler: BuiltinHandler) -> None:
        """Register a core built-in tool (ADR-0039).

        A setter (like ``set_url_provider``) so wiring that needs the registry — e.g. the
        ``now`` tool's calendar-timezone lookup — can be attached after construction.
        """
        self._builtins[name] = (spec, handler)

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
        # Offer core built-in tools alongside the modules' (ADR-0039). They respect the same
        # disabled-tools filter; a module tool of the same name would already own the route,
        # so built-ins never shadow a module.
        for name, (spec, _handler) in self._builtins.items():
            if name in disabled or name in route:
                continue
            specs.append(spec)
            route[name] = _BUILTIN_URL
        return specs, route

    async def call(self, name: str, arguments: dict[str, Any], url: str, *, tenant: str) -> str:
        """Call ``name`` on the module at ``url`` (or a core built-in) and return its text.

        ``tenant`` scopes a core built-in's access to per-tenant state; it is unused for a
        module call (the module resolves identity through the platform API).

        Every hop is bounded by :data:`_CALL_TIMEOUT_S` (connect, ``initialize``, and the
        tool RPC) so an unresponsive module can never hang the caller (#472).

        Raises:
            ToolCallError: when the tool ran but reported failure (MCP ``isError``) —
                the message is the tool's own error text (#435).
            ModuleUnreachableError: when the module refuses, drops, or exceeds the timeout
                before the tool could run — mapped to a controlled 502 by the HTTP layer.
        """
        if url == _BUILTIN_URL:
            return await self._builtins[name][1](arguments, tenant)
        timeout = timedelta(seconds=_CALL_TIMEOUT_S)
        try:
            async with (
                streamablehttp_client(url, timeout=_CALL_TIMEOUT_S) as (read, write, _),
                ClientSession(read, write, read_timeout_seconds=timeout) as session,
            ):
                await session.initialize()
                result = await session.call_tool(name, arguments, read_timeout_seconds=timeout)
        except (McpError, httpx.HTTPError, OSError, ExceptionGroup) as exc:
            # The streamable-HTTP client runs its transport in an anyio task group, so a
            # refused/dropped connection or an RPC read timeout escapes wrapped in a
            # (possibly nested) ``ExceptionGroup`` — not a bare httpx/McpError. Normalize
            # every such failure to one domain type so callers don't have to unwrap groups
            # (a bare httpx/OSError is caught too, for a failure raised before the task
            # group starts). ``ExceptionGroup`` — never ``BaseExceptionGroup`` — so a
            # cancellation group (CancelledError is a BaseException) still propagates (#472).
            log.warning("module call unreachable", tool=name, url=url, error=repr(exc))
            raise ModuleUnreachableError(f"module for tool {name!r} is unreachable") from exc
        # The ``isError`` → ``ToolCallError`` raise lives *outside* the transport block on
        # purpose: raised inside, anyio would wrap it in an ExceptionGroup and callers'
        # ``except ToolCallError`` would silently miss it (the #472 failure the mocked #435
        # tests couldn't see). Out here the result is in hand and the group is unwound.
        if result.isError:
            raise ToolCallError(_text(result.content) or f"tool {name!r} failed")
        return _text(result.content)
