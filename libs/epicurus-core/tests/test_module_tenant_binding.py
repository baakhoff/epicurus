"""The tenant crosses the MCP hop: ``EpicurusModule`` binds the caller's tenant (#917).

These tests serve a real ``EpicurusModule`` over streamable HTTP on a local port (uvicorn,
no Docker) and call it with the SDK's own client, because the hazard #917 names only exists
over the wire: on a **stateful** streamable-HTTP session the tool runs in the session's
long-lived task, which a ContextVar set while handling the HTTP request never reaches. An
in-process ``call_tool`` cannot show that, so the binding is proven here against a served
module — on the legacy handshake (a stateful session with an ``mcp-session-id``) and on the
SDK's default ``auto`` negotiation alike.

Covered: two tenants calling concurrently each see only their own; the header absent (an
older core) leaves the context unbound so the module's default applies; an empty header is
the same as absent; a malformed header is refused before the tool runs; the binding never
leaks into the next call on the same session; and ``PlatformClient`` — inside the tool —
follows the bound tenant all the way back into a (fake) core over the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from sse_starlette.sse import AppStatus

from epicurus_core import (
    TENANT_HEADER,
    EpicurusModule,
    ModuleConfigCache,
    PlatformClient,
    TenantError,
    bound_tenant,
    current_tenant,
    reset_current_tenant,
    set_current_tenant,
)

UNBOUND = "<unbound>"
FALLBACK = "deployment-default"
MODES = ["legacy", "auto"]


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class _InProcessServer(uvicorn.Server):
    """A uvicorn server that never takes over the process's signal handlers.

    ``uvicorn.Server.serve()`` installs its own ``handle_exit`` as the SIGINT/SIGTERM handler
    while it runs. sse-starlette (the SSE layer under the MCP SDK's streamable-HTTP transport)
    polls for exactly that: its shutdown watcher finds the server through the SIGTERM handler,
    and once it sees ``server.should_exit`` it sets the **process-global**
    ``AppStatus.should_exit = True`` — permanently. In production that is the intended drain at
    shutdown, one server per process. In a test process that starts and stops many servers, a
    poll that lands while one is stopping latches the flag, and every later SSE response —
    each legacy-handshake ``initialize`` — closes before its answer ("SSE stream ended without
    a response"). A server embedded in a test owns no process signals, so it installs none.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@contextlib.asynccontextmanager
async def _serve(app: Any) -> AsyncIterator[str]:
    """Serve *app* on an ephemeral localhost port; yield its base URL."""
    port = _free_port()
    server = _InProcessServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(250):
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - only trips if uvicorn never comes up
            raise RuntimeError("uvicorn did not start in time")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
    # Stopping this server must not have shut SSE down for the rest of the process.
    assert not AppStatus.should_exit


class _Probe:
    """A module whose tools report the tenant they ran under."""

    def __init__(self, platform_url: str = "http://core.invalid") -> None:
        self.ran: list[str] = []
        self.module = EpicurusModule("probe")
        platform = PlatformClient(platform_url, FALLBACK, module="probe")
        config = ModuleConfigCache(platform, ttl_s=60.0)
        module = self.module

        @module.tool()
        async def whoami() -> str:
            self.ran.append("whoami")
            return bound_tenant() or UNBOUND

        @module.tool()
        def whoami_sync() -> str:
            return bound_tenant() or UNBOUND

        @module.tool()
        async def whoami_slowly(delay: float) -> str:
            # Read, yield to the loop while another call runs, read again: a binding shared
            # between concurrent calls would show up as a mismatch here.
            before = bound_tenant() or UNBOUND
            await asyncio.sleep(delay)
            after = bound_tenant() or UNBOUND
            return f"{before}|{after}"

        @module.tool()
        async def platform_tenant() -> str:
            # What a module's PlatformClient will name on its next call into the core.
            return platform.tenant_id

        @module.tool()
        async def platform_config() -> str:
            # A real round trip into the (fake) core, through the per-tenant cache.
            return str((await config.get()).get("owner"))

    def app(self) -> Any:
        return self.module.http_app()


@contextlib.asynccontextmanager
async def _session(
    url: str, *, tenant: str | None, mode: str = "legacy"
) -> AsyncIterator[tuple[Client, httpx2.AsyncClient]]:
    """One MCP session whose HTTP client stamps *tenant* (or nothing) on every request."""
    headers = {TENANT_HEADER: tenant} if tenant is not None else {}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=300.0)) as http,
        Client(streamable_http_client(url, http_client=http), mode=mode) as client,
    ):
        yield client, http


def _text(result: CallToolResult) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


async def _call(
    url: str, tool: str, args: dict[str, Any] | None = None, *, tenant: str | None, mode: str
) -> CallToolResult:
    async with _session(url, tenant=tenant, mode=mode) as (client, _http):
        return await client.call_tool(tool, args or {})


# ── The binding, over the wire ─────────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
async def test_the_named_tenant_is_bound_for_the_tool(mode: str) -> None:
    probe = _Probe()
    async with _serve(probe.app()) as url:
        result = await _call(url, "whoami", tenant="acme", mode=mode)
    assert not result.is_error
    assert _text(result) == "acme"


@pytest.mark.parametrize("mode", MODES)
async def test_a_sync_tool_sees_the_binding_too(mode: str) -> None:
    probe = _Probe()
    async with _serve(probe.app()) as url:
        result = await _call(url, "whoami_sync", tenant="acme", mode=mode)
    assert _text(result) == "acme"


@pytest.mark.parametrize("mode", MODES)
async def test_two_tenants_calling_concurrently_each_see_only_their_own(mode: str) -> None:
    probe = _Probe()
    async with _serve(probe.app()) as url:

        async def one(tenant: str, delay: float) -> str:
            return _text(
                await _call(url, "whoami_slowly", {"delay": delay}, tenant=tenant, mode=mode)
            )

        # Interleaved on purpose: each call sleeps while the others run, so every pair of
        # them overlaps inside the module.
        tenants = ["acme", "globex"] * 6
        results = await asyncio.gather(
            *(one(t, 0.05 + 0.01 * (i % 3)) for i, t in enumerate(tenants))
        )
    assert results == [f"{t}|{t}" for t in tenants]


@pytest.mark.parametrize("mode", MODES)
async def test_no_header_leaves_the_context_unbound(mode: str) -> None:
    """A core older than #917 sends no header: the tool resolves its own default."""
    probe = _Probe()
    async with _serve(probe.app()) as url:
        result = await _call(url, "whoami", tenant=None, mode=mode)
        platform = await _call(url, "platform_tenant", tenant=None, mode=mode)
    assert _text(result) == UNBOUND
    assert _text(platform) == FALLBACK


async def test_an_empty_header_is_the_same_as_none() -> None:
    probe = _Probe()
    async with _serve(probe.app()) as url:
        result = await _call(url, "whoami", tenant="", mode="legacy")
    assert not result.is_error
    assert _text(result) == UNBOUND


@pytest.mark.parametrize("bad", ["Acme", "-acme", "acme_corp", "a" * 64, "acme corp"])
async def test_a_malformed_header_is_refused_before_the_tool_runs(bad: str) -> None:
    probe = _Probe()
    async with _serve(probe.app()) as url:
        result = await _call(url, "whoami", tenant=bad, mode="legacy")
    assert result.is_error
    assert "refused" in _text(result)
    assert "invalid tenant id" in _text(result)
    assert probe.ran == []


@pytest.mark.parametrize("mode", MODES)
async def test_the_binding_does_not_leak_into_the_next_call_on_the_same_session(
    mode: str,
) -> None:
    probe = _Probe()
    async with (
        _serve(probe.app()) as url,
        _session(url, tenant="acme", mode=mode) as (
            client,
            http,
        ),
    ):
        first = await client.call_tool("whoami", {})
        del http.headers[TENANT_HEADER]
        second = await client.call_tool("whoami", {})
        http.headers[TENANT_HEADER] = "globex"
        third = await client.call_tool("whoami", {})
        http.headers[TENANT_HEADER] = "acme"
        fourth = await client.call_tool("platform_tenant", {})
    assert [_text(r) for r in (first, second, third, fourth)] == [
        "acme",
        UNBOUND,
        "globex",
        "acme",
    ]


# ── PlatformClient follows the tenant back into the core ───────────────────────


def _fake_core() -> tuple[FastAPI, list[str | None]]:
    """A core that answers a module's settings read with the tenant it was asked for."""
    asked: list[str | None] = []
    app = FastAPI()

    @app.get("/platform/v1/modules/{name}/config")
    async def config(name: str, tenant_id: str | None = None) -> dict[str, Any]:
        asked.append(tenant_id)
        return {"owner": tenant_id}

    return app, asked


async def test_the_module_reaches_the_calling_tenants_data_in_the_core() -> None:
    """Core → module (header) → tool → PlatformClient → core (``tenant_id``), end to end.

    Two tenants concurrently, then the same two again: each gets its own settings back, and
    the second round is served from each tenant's own cache entry — never the other's.
    """
    core_app, asked = _fake_core()
    async with _serve(core_app) as core_url:
        probe = _Probe(platform_url=core_url)
        async with _serve(probe.app()) as url:

            async def one(tenant: str | None) -> str:
                return _text(await _call(url, "platform_config", tenant=tenant, mode="legacy"))

            first = list(await asyncio.gather(one("acme"), one("globex"), one(None)))
            second = list(await asyncio.gather(one("globex"), one("acme"), one(None)))
    assert first == ["acme", "globex", FALLBACK]
    assert second == ["globex", "acme", FALLBACK]
    # One read per tenant: the second round came from three separate cache entries.
    assert sorted(a or "" for a in asked) == sorted(["acme", "globex", FALLBACK])


# ── In-process ─────────────────────────────────────────────────────────────────


async def test_in_process_call_binds_the_given_tenant_and_restores_the_outer_one() -> None:
    probe = _Probe()
    outer = set_current_tenant("outer")
    try:
        content, _ = await probe.module.call_tool("whoami", {}, tenant="acme")
        assert [b.text for b in content if isinstance(b, TextContent)] == ["acme"]
        assert current_tenant() == "outer"
        # No tenant given: the call runs in the caller's context unchanged.
        content, _ = await probe.module.call_tool("whoami", {})
        assert [b.text for b in content if isinstance(b, TextContent)] == ["outer"]
    finally:
        reset_current_tenant(outer)
    assert bound_tenant() is None


async def test_in_process_call_refuses_a_malformed_tenant() -> None:
    probe = _Probe()
    with pytest.raises(TenantError):
        await probe.module.call_tool("whoami", {}, tenant="Not Valid")
    assert probe.ran == []
