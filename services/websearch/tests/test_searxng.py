"""Unit tests for the SearXNG client.

Uses httpx's MockTransport so no real SearXNG instance is needed.
"""

from __future__ import annotations

import httpx
import pytest

from epicurus_websearch.searxng import SearXNGClient


def _make_client(responses: dict[str, httpx.Response]) -> SearXNGClient:
    """Build a SearXNGClient backed by a mock transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in responses:
            return responses[path]
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = SearXNGClient("http://searxng:8080")
    client._client = httpx.AsyncClient(transport=transport, base_url="http://searxng:8080")
    return client


def _search_response(results: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(200, json={"results": results})


async def test_search_returns_results() -> None:
    raw = [
        {"title": "A", "url": "https://a.com", "content": "Snippet A", "engine": "google"},
        {"title": "B", "url": "https://b.com", "content": "Snippet B", "engine": "bing"},
    ]
    client = _make_client({"/search": _search_response(raw)})
    results = await client.search("hello")
    assert len(results) == 2
    assert results[0]["title"] == "A"
    assert results[0]["url"] == "https://a.com"
    assert results[0]["snippet"] == "Snippet A"
    assert results[0]["engine"] == "google"


async def test_search_respects_num_results() -> None:
    raw = [
        {"title": str(i), "url": f"https://{i}.com", "content": "", "engine": "x"}
        for i in range(10)
    ]
    client = _make_client({"/search": _search_response(raw)})
    results = await client.search("q", num_results=3)
    assert len(results) == 3


async def test_search_skips_results_without_url() -> None:
    raw = [
        {"title": "No URL", "url": "", "content": "text", "engine": "g"},
        {"title": "Has URL", "url": "https://ok.com", "content": "text", "engine": "g"},
    ]
    client = _make_client({"/search": _search_response(raw)})
    results = await client.search("q")
    assert len(results) == 1
    assert results[0]["url"] == "https://ok.com"


async def test_search_returns_empty_on_empty_results() -> None:
    client = _make_client({"/search": _search_response([])})
    results = await client.search("q")
    assert results == []


async def test_search_handles_missing_fields() -> None:
    raw = [{"url": "https://x.com"}]
    client = _make_client({"/search": _search_response(raw)})
    results = await client.search("q")
    assert results[0]["title"] == ""
    assert results[0]["snippet"] == ""
    assert results[0]["engine"] == ""


async def test_health_check_true_on_200() -> None:
    client = _make_client({"/healthz": httpx.Response(200, text="OK")})
    assert await client.health_check() is True


async def test_health_check_false_on_error() -> None:
    client = _make_client({})
    assert await client.health_check() is False


async def test_search_raises_on_http_error() -> None:
    client = _make_client({"/search": httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        await client.search("q")


async def test_engines_param_forwarded() -> None:
    """engines= must be passed as a query parameter when configured."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return _search_response([])

    transport = httpx.MockTransport(handler)
    client = SearXNGClient("http://searxng:8080", engines="google,bing")
    client._client = httpx.AsyncClient(transport=transport, base_url="http://searxng:8080")

    await client.search("test")
    assert "engines=google%2Cbing" in captured[0] or "engines=google,bing" in captured[0]


async def test_no_engines_param_when_empty() -> None:
    """engines= must NOT appear in the request when the setting is empty."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return _search_response([])

    transport = httpx.MockTransport(handler)
    client = SearXNGClient("http://searxng:8080", engines="")
    client._client = httpx.AsyncClient(transport=transport, base_url="http://searxng:8080")

    await client.search("test")
    assert "engines" not in captured[0]
