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
    raw: list[dict[str, object]] = [
        {"title": "A", "url": "https://a.com", "content": "Snippet A", "engine": "google"},
        {"title": "B", "url": "https://b.com", "content": "Snippet B", "engine": "bing"},
    ]
    client = _make_client({"/search": _search_response(raw)})
    outcome = await client.search("hello")
    assert len(outcome.results) == 2
    assert outcome.results[0]["title"] == "A"
    assert outcome.results[0]["url"] == "https://a.com"
    assert outcome.results[0]["snippet"] == "Snippet A"
    assert outcome.results[0]["engine"] == "google"
    assert outcome.unresponsive_engines == []


async def test_search_respects_num_results() -> None:
    raw: list[dict[str, object]] = [
        {"title": str(i), "url": f"https://{i}.com", "content": "", "engine": "x"}
        for i in range(10)
    ]
    client = _make_client({"/search": _search_response(raw)})
    outcome = await client.search("q", num_results=3)
    assert len(outcome.results) == 3


async def test_search_skips_results_without_url() -> None:
    raw: list[dict[str, object]] = [
        {"title": "No URL", "url": "", "content": "text", "engine": "g"},
        {"title": "Has URL", "url": "https://ok.com", "content": "text", "engine": "g"},
    ]
    client = _make_client({"/search": _search_response(raw)})
    outcome = await client.search("q")
    assert len(outcome.results) == 1
    assert outcome.results[0]["url"] == "https://ok.com"


async def test_search_returns_empty_on_empty_results() -> None:
    client = _make_client({"/search": _search_response([])})
    outcome = await client.search("q")
    assert outcome.results == []
    assert outcome.unresponsive_engines == []


async def test_search_handles_missing_fields() -> None:
    raw: list[dict[str, object]] = [{"url": "https://x.com"}]
    client = _make_client({"/search": _search_response(raw)})
    outcome = await client.search("q")
    assert outcome.results[0]["title"] == ""
    assert outcome.results[0]["snippet"] == ""
    assert outcome.results[0]["engine"] == ""


# ── unresponsive_engines / degraded-vs-empty (#936) ───────────────────────────────────


async def test_search_parses_unresponsive_engines() -> None:
    """A 200 with results:[] and unresponsive_engines non-empty is a degraded search, not a
    genuine empty one — the whole point of #936."""
    body = {
        "results": [],
        "unresponsive_engines": [["google", "timeout"], ["bing", "blocked"]],
        "number_of_results": 0,
    }
    client = _make_client({"/search": httpx.Response(200, json=body)})
    outcome = await client.search("q")
    assert outcome.results == []
    assert outcome.unresponsive_engines == [("google", "timeout"), ("bing", "blocked")]


async def test_search_reports_unresponsive_engines_alongside_partial_results() -> None:
    """Some engines answered, some didn't — the results still travel, but so does the caveat."""
    body = {
        "results": [{"title": "A", "url": "https://a.com", "content": "S", "engine": "duckduckgo"}],
        "unresponsive_engines": [["google", "too many requests"]],
    }
    client = _make_client({"/search": httpx.Response(200, json=body)})
    outcome = await client.search("q")
    assert len(outcome.results) == 1
    assert outcome.unresponsive_engines == [("google", "too many requests")]


async def test_search_no_unresponsive_engines_field_is_a_genuine_empty() -> None:
    """SearXNG omitting the field entirely (older instance?) must not be treated as degraded."""
    client = _make_client({"/search": _search_response([])})
    outcome = await client.search("q")
    assert outcome.unresponsive_engines == []


async def test_search_number_of_results_is_parsed() -> None:
    body = {"results": [], "unresponsive_engines": [], "number_of_results": 42}
    client = _make_client({"/search": httpx.Response(200, json=body)})
    outcome = await client.search("q")
    assert outcome.number_of_results == 42


async def test_last_unresponsive_engines_starts_empty() -> None:
    client = _make_client({"/search": _search_response([])})
    assert client.last_unresponsive_engines == []


async def test_last_unresponsive_engines_reflects_the_most_recent_search() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [], "unresponsive_engines": [["google", "timeout"]]},
        )

    transport = httpx.MockTransport(handler)
    client = SearXNGClient("http://searxng:8080")
    client._client = httpx.AsyncClient(transport=transport, base_url="http://searxng:8080")

    await client.search("q")
    assert client.last_unresponsive_engines == [("google", "timeout")]


async def test_last_unresponsive_engines_clears_after_a_healthy_search() -> None:
    responses = iter(
        [
            httpx.Response(200, json={"results": [], "unresponsive_engines": [["google", "x"]]}),
            httpx.Response(200, json={"results": [], "unresponsive_engines": []}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    transport = httpx.MockTransport(handler)
    client = SearXNGClient("http://searxng:8080")
    client._client = httpx.AsyncClient(transport=transport, base_url="http://searxng:8080")

    await client.search("q")
    assert client.last_unresponsive_engines == [("google", "x")]
    await client.search("q")
    assert client.last_unresponsive_engines == []


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
