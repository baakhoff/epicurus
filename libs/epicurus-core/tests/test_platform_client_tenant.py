"""``PlatformClient`` resolves its tenant per call (#917).

Order: a :meth:`~PlatformClient.for_tenant` pin, else the tenant bound to the calling
context (what ``EpicurusModule`` binds around a tool), else the tenant the client was built
with. Every platform call sends the resolved one, and ``ModuleConfigCache`` keeps one entry
per tenant so a tenant's settings — or its last-known-good fallback — never reach another.

The core is faked at the HTTP layer with ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx
import pytest

from epicurus_core import (
    ModuleConfigCache,
    PlatformClient,
    PlatformMessage,
    TenantError,
    reset_current_tenant,
    set_current_tenant,
)

BUILT = "local"
BOUND = "acme"
PINNED = "globex"


class _Core:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = _answer

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def tenants(self) -> list[str | None]:
        """The tenant each request named — query parameter or JSON body."""
        out: list[str | None] = []
        for r in self.requests:
            if "tenant_id" in r.url.params:
                out.append(r.url.params["tenant_id"])
            elif r.content:
                out.append(json.loads(r.content).get("tenant_id"))
            else:
                out.append(None)
        return out


def _answer(request: httpx.Request) -> httpx.Response:
    """A plausible 200 for every platform route the client calls."""
    path = request.url.path
    entry = {"path": "a.md", "name": "a.md", "kind": "file", "size": 1, "mtime": 0.0}
    if path.endswith("/embed"):
        return httpx.Response(200, json={"embeddings": [[0.0]]})
    if path.endswith("/chat"):
        return httpx.Response(200, json={"content": "ok", "model": "m"})
    if "/oauth/" in path:
        return httpx.Response(200, json={"access_token": "t"})
    if path.endswith("/timezone"):
        return httpx.Response(200, json={"timezone": "UTC"})
    if path.endswith("/collections/prefs"):
        return httpx.Response(200, json={"enabled": [], "active": None})
    if path.endswith("/suggestions-enabled"):
        return httpx.Response(200, json={"enabled": True})
    if "/models/" in path:
        return httpx.Response(200, json={"model": None})
    if path.endswith("/web-search"):
        return httpx.Response(200, json={"results": [], "backend": "openrouter", "model": "m"})
    if path.endswith("/docs"):
        return httpx.Response(200, json={"documents": []})
    if path in ("/platform/v1/files/list", "/platform/v1/files/search"):
        return httpx.Response(200, json={"entries": [entry]})
    if path == "/platform/v1/files/read":
        return httpx.Response(200, json={"content": "x"})
    if path == "/platform/v1/files" and request.method == "DELETE":
        return httpx.Response(200, json={"deleted": True})
    if path.startswith("/platform/v1/files"):
        return httpx.Response(200, json=entry)
    if path == "/platform/v1/modules":
        return httpx.Response(200, json=[])
    return httpx.Response(200, json={})


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Core]:
    fake = _Core()
    real = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake._handle)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    yield fake


def _client() -> PlatformClient:
    return PlatformClient("http://core:8080", BUILT, module="notes")


Call = Callable[[PlatformClient], Awaitable[object]]

#: Every PlatformClient method that reaches the core — each must name the resolved tenant.
CALLS: dict[str, Call] = {
    "embed": lambda c: c.embed(["x"]),
    "chat": lambda c: c.chat([PlatformMessage(role="user", content="hi")]),
    "get_oauth_token": lambda c: c.get_oauth_token("google"),
    "get_module_model": lambda c: c.get_module_model("embedding"),
    "get_suggestions_enabled": lambda c: c.get_suggestions_enabled(),
    "get_timezone": lambda c: c.get_timezone(),
    "get_collections": lambda c: c.get_collections(),
    "get_module_config": lambda c: c.get_module_config(),
    "web_search": lambda c: c.web_search("q"),
    "list_modules": lambda c: c.list_modules(),
    "get_module_docs": lambda c: c.get_module_docs("echo"),
    "files_list": lambda c: c.files_list(""),
    "files_search": lambda c: c.files_search("a"),
    "files_read": lambda c: c.files_read("a.md"),
    "files_write": lambda c: c.files_write("a.md", "x"),
    "files_stat": lambda c: c.files_stat("a.md"),
    "files_delete": lambda c: c.files_delete("a.md"),
    "files_make_dir": lambda c: c.files_make_dir("d"),
    "files_move": lambda c: c.files_move("a.md", "b.md"),
}


# ── Resolution order ───────────────────────────────────────────────────────────


def test_unbound_resolves_to_the_built_tenant() -> None:
    assert _client().tenant_id == BUILT


def test_a_bound_context_wins_over_the_built_tenant() -> None:
    client = _client()
    token = set_current_tenant(BOUND)
    try:
        assert client.tenant_id == BOUND
    finally:
        reset_current_tenant(token)
    assert client.tenant_id == BUILT


def test_a_pin_wins_over_the_bound_context_and_leaves_the_original_alone() -> None:
    client = _client()
    pinned = client.for_tenant(PINNED)
    token = set_current_tenant(BOUND)
    try:
        assert pinned.tenant_id == PINNED
        assert client.tenant_id == BOUND
    finally:
        reset_current_tenant(token)
    assert pinned.tenant_id == PINNED
    assert client.tenant_id == BUILT


def test_a_pin_to_a_malformed_tenant_is_refused() -> None:
    with pytest.raises(TenantError):
        _client().for_tenant("Not Valid")


async def test_a_pin_keeps_the_module_name(core: _Core) -> None:
    await _client().for_tenant(PINNED).get_module_config()
    assert core.requests[0].url.path == "/platform/v1/modules/notes/config"


# ── Every call names the resolved tenant ───────────────────────────────────────


@pytest.mark.parametrize("name", sorted(CALLS))
async def test_every_call_names_the_resolved_tenant(core: _Core, name: str) -> None:
    call = CALLS[name]
    client = _client()
    await call(client)
    token = set_current_tenant(BOUND)
    try:
        await call(client)
        await call(client.for_tenant(PINNED))
    finally:
        reset_current_tenant(token)
    assert core.tenants() == [BUILT, BOUND, PINNED]


async def test_one_client_serves_two_concurrent_tenants(core: _Core) -> None:
    client = _client()

    async def as_tenant(tenant: str) -> None:
        token = set_current_tenant(tenant)
        try:
            await asyncio.sleep(0.01)
            await client.get_module_config()
        finally:
            reset_current_tenant(token)

    # Each task runs in its own context copy, as each MCP tool call does.
    await asyncio.gather(*(as_tenant(t) for t in ["acme", "globex", "acme", "globex"]))
    assert sorted(t or "" for t in core.tenants()) == ["acme", "acme", "globex", "globex"]


# ── ModuleConfigCache is per tenant ────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _by_tenant(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"owner": request.url.params.get("tenant_id")})


async def _get_as(cache: ModuleConfigCache, tenant: str | None) -> dict[str, Any]:
    if tenant is None:
        return await cache.get()
    token = set_current_tenant(tenant)
    try:
        return await cache.get()
    finally:
        reset_current_tenant(token)


async def test_cache_answers_each_tenant_from_its_own_entry(core: _Core) -> None:
    core.handler = _by_tenant
    clock = _Clock()
    cache = ModuleConfigCache(_client(), ttl_s=15.0, clock=clock)
    assert await _get_as(cache, BOUND) == {"owner": BOUND}
    # A fresh entry for one tenant is never an answer for another.
    assert await _get_as(cache, PINNED) == {"owner": PINNED}
    assert await _get_as(cache, None) == {"owner": BUILT}
    clock.now += 5
    assert await _get_as(cache, BOUND) == {"owner": BOUND}
    assert await _get_as(cache, PINNED) == {"owner": PINNED}
    assert len(core.requests) == 3


async def test_cache_last_known_good_is_per_tenant(core: _Core) -> None:
    core.handler = _by_tenant
    clock = _Clock()
    cache = ModuleConfigCache(_client(), ttl_s=15.0, clock=clock)
    await _get_as(cache, BOUND)

    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    core.handler = down
    clock.now += 20
    # The core is down: the tenant that had an answer keeps it, the one that never did gets
    # ``{}`` (its env defaults) — never the other tenant's last-known settings.
    assert await _get_as(cache, BOUND) == {"owner": BOUND}
    assert await _get_as(cache, PINNED) == {}


async def test_cache_invalidate_forgets_every_tenant(core: _Core) -> None:
    core.handler = _by_tenant
    cache = ModuleConfigCache(_client(), clock=_Clock())
    await _get_as(cache, BOUND)
    await _get_as(cache, PINNED)
    cache.invalidate()
    await _get_as(cache, BOUND)
    await _get_as(cache, PINNED)
    assert len(core.requests) == 4
