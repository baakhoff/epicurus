"""``PlatformClient.web_search`` / ``get_module_config`` and ``ModuleConfigCache`` (#984).

The core is faked at the HTTP layer with ``httpx.MockTransport`` so the exact paths, query
parameters and bodies the client sends are pinned, along with how each core answer is read.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from epicurus_core import (
    ModuleConfigCache,
    PlatformClient,
    PlatformError,
    WebSearchResult,
)

Handler = Callable[[httpx.Request], httpx.Response]


class _Core:
    """Routes every ``httpx.AsyncClient`` the platform client opens to ``handler``."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = lambda _: httpx.Response(200, json={})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


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
    return PlatformClient("http://core:8080", "acme", module="websearch")


# ── web_search ───────────────────────────────────────────────────────────────────


async def test_web_search_posts_the_query_with_the_tenant(core: _Core) -> None:
    core.handler = lambda _: httpx.Response(
        200,
        json={
            "results": [
                {"title": "T", "url": "https://t.example", "snippet": "s", "engine": "OpenRouter"}
            ],
            "searched": True,
            "backend": "openrouter",
            "model": "openai/gpt-4.1-nano",
            "search_engine": "exa",
        },
    )
    result = await _client().web_search("tidal", max_results=4)

    request = core.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/platform/v1/web-search"
    assert json.loads(request.content) == {"query": "tidal", "max_results": 4, "tenant_id": "acme"}
    assert isinstance(result, WebSearchResult)
    assert result.results[0].engine == "OpenRouter"
    assert result.searched is True


async def test_web_search_raises_the_cores_structured_refusal(core: _Core) -> None:
    core.handler = lambda _: httpx.Response(
        409,
        json={
            "detail": {
                "code": "openrouter_key_missing",
                "message": "No OpenRouter API key is stored.",
                "backend": "openrouter",
            }
        },
    )
    with pytest.raises(PlatformError) as err:
        await _client().web_search("q")
    assert err.value.status == 409
    assert err.value.code == "openrouter_key_missing"
    assert err.value.message == "No OpenRouter API key is stored."
    assert str(err.value) == "No OpenRouter API key is stored."


async def test_a_plain_string_detail_keeps_its_text(core: _Core) -> None:
    core.handler = lambda _: httpx.Response(502, json={"detail": "bad gateway upstream"})
    with pytest.raises(PlatformError) as err:
        await _client().web_search("q")
    assert (err.value.code, err.value.message) == ("http_502", "bad gateway upstream")


async def test_a_non_json_error_body_still_names_the_status(core: _Core) -> None:
    core.handler = lambda _: httpx.Response(504, text="<html>Gateway Timeout</html>")
    with pytest.raises(PlatformError) as err:
        await _client().web_search("q")
    assert err.value.code == "http_504"
    assert err.value.message


# ── get_module_config ────────────────────────────────────────────────────────────


async def test_get_module_config_reads_this_modules_stored_settings(core: _Core) -> None:
    core.handler = lambda _: httpx.Response(200, json={"websearch_backend": "openrouter"})
    assert await _client().get_module_config() == {"websearch_backend": "openrouter"}
    request = core.requests[0]
    assert request.url.path == "/platform/v1/modules/websearch/config"
    assert request.url.params["tenant_id"] == "acme"


async def test_get_module_config_needs_the_module_name() -> None:
    with pytest.raises(ValueError):
        await PlatformClient("http://core:8080", "acme").get_module_config()


# ── ModuleConfigCache ────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


async def test_cache_answers_from_memory_within_the_ttl(core: _Core, clock: _Clock) -> None:
    core.handler = lambda _: httpx.Response(200, json={"a": 1})
    cache = ModuleConfigCache(_client(), ttl_s=15.0, clock=clock)
    assert await cache.get() == {"a": 1}
    clock.now += 10
    assert await cache.get() == {"a": 1}
    assert len(core.requests) == 1

    core.handler = lambda _: httpx.Response(200, json={"a": 2})
    clock.now += 6
    assert await cache.get() == {"a": 2}
    assert len(core.requests) == 2


async def test_cache_keeps_the_last_good_answer_when_the_core_is_down(
    core: _Core, clock: _Clock
) -> None:
    core.handler = lambda _: httpx.Response(200, json={"a": 1})
    cache = ModuleConfigCache(_client(), ttl_s=15.0, clock=clock)
    await cache.get()

    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    core.handler = down
    clock.now += 20
    assert await cache.get() == {"a": 1}


async def test_cache_with_no_answer_yet_falls_back_to_empty(core: _Core, clock: _Clock) -> None:
    core.handler = lambda _: httpx.Response(500, json={"detail": "boom"})
    cache = ModuleConfigCache(_client(), clock=clock)
    assert await cache.get() == {}
    # A failure is not cached as an answer: the next read asks again.
    core.handler = lambda _: httpx.Response(200, json={"a": 3})
    assert await cache.get() == {"a": 3}


async def test_invalidate_forces_a_fresh_read(core: _Core, clock: _Clock) -> None:
    core.handler = lambda _: httpx.Response(200, json={"a": 1})
    cache = ModuleConfigCache(_client(), clock=clock)
    await cache.get()
    cache.invalidate()
    await cache.get()
    assert len(core.requests) == 2


async def test_the_cached_dict_cannot_be_mutated_by_a_caller(core: _Core, clock: _Clock) -> None:
    core.handler = lambda _: httpx.Response(200, json={"a": 1})
    cache = ModuleConfigCache(_client(), clock=clock)
    first = await cache.get()
    first["a"] = 99
    assert await cache.get() == {"a": 1}
